#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

// Client for the lipgloss print service.
//
// Mirrors LipglossClient in packages/illusion-core/src/illusion_core/clients.py,
// which is the authority on the wire format, when an endpoint changes there,
// it changes here. Only the two read endpoints are implemented so far; the
// print side follows once the queue view is real.
//
// Nothing here is on a hot path. lipgloss is a USB label printer on the far
// end, so a poll every second is already far more often than anything can
// change, and the whole point of putting it on a worker thread is that the
// frame loop never waits on a socket.

namespace lipgloss {

// One row of GET /queue's "jobs" array. The keys are upper-case on the wire
// because the same rows feed illusion's terminal table renderer.
struct Job {
    std::string jobId;
    std::string description;
    std::string labels;  // "remaining/total", pre-formatted by the service
    std::string source;
    std::string state;   // "Printing", "Paused" or "Waiting"
};

// Everything the UI draws, as of one poll. Copied out under a lock rather than
// read field by field, so a frame can never show half of one poll and half of
// the next.
struct Snapshot {
    // False until the first poll completes, and after any poll that failed.
    bool reachable = false;

    // Why the last attempt failed, for the status line. Empty when reachable.
    std::string error;

    // Set when the transport worked but the service refused us -- a 401 from a
    // token mismatch is the overwhelmingly likely cause and is worth saying
    // plainly, because it looks identical to "down" in every other respect.
    bool unauthorized = false;

    // GET /health. Unauthenticated on purpose over there, so these can be
    // populated even while the token is wrong -- which is exactly what makes
    // the distinction above visible.
    std::string version;
    std::string printerPort;
    std::string model;
    long long uptimeMs = 0;

    // GET /queue.
    std::string title;        // "Print Queue: Idle" and friends
    std::string description;  // may contain newlines
    bool paused = false;
    std::string pauseReason;
    int pendingJobs = 0;
    int pendingLabels = 0;
    std::vector<Job> jobs;

    // When this snapshot was taken, for the "last updated" line. Default-
    // constructed means "never polled".
    std::chrono::steady_clock::time_point polled {};
};

// lipgloss rejects anything above this with a 422 (MAX_COPIES in
// packages/lipgloss/src/lipgloss/print_queue.py). Mirrored so the UI can clamp
// rather than let a request be refused after the fact.
constexpr int kMaxCopies = 100;

// What POST /print takes. The style must already be resolved -- lipgloss knows
// label_1_line and label_2_line, not the "label" the user picked; see
// styles::resolve in main.cpp for where that happens.
struct PrintRequest {
    std::string style;
    std::string sku;
    std::string line1;
    std::string line2;
    int copies = 1;
};

// The outcome of the last submitted print. One value rather than a list: the
// UI submits one at a time and only reports the most recent.
struct ActionResult {
    enum class State {
        Idle,
        Pending,
        Ok,
        Failed,
    };

    State state = State::Idle;

    // lipgloss's own wording where it gave any -- it explains queue state
    // better than anything invented here would.
    std::string message;

    long long jobId = -1;

    // A job accepted onto a paused queue is not printing. lipgloss reports
    // this separately for exactly that reason, so "queued" is not mistaken for
    // "printed".
    bool queuePaused = false;
};

// lipgloss clamps this to PREVIEW_MAX_SCALE (8) in label_maker.py. 3 is its
// default and is what the bot asks for.
constexpr int kPreviewScale = 3;

// The PNG POST /preview hands back, and what went wrong if it did not.
struct PreviewResult {
    enum class State {
        Idle,
        Pending,
        Ready,
        Failed,
    };

    State state = State::Idle;

    // Raw PNG bytes. std::string rather than a vector because that is what the
    // HTTP layer fills and it holds arbitrary bytes perfectly well.
    std::string png;

    std::string error;

    // Bumped every time png is replaced. The UI uploads a texture only when
    // this changes, rather than decoding the same PNG every frame.
    unsigned long long serial = 0;
};

class Client {
public:
    Client();
    ~Client();

    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;

    // Starts the worker. Safe to call before an endpoint is set: with no URL
    // configured the worker simply reports that and sleeps.
    void start();

    // Joins the worker. Called from the shutdown path; also called by the
    // destructor, so an early return does not leak a thread.
    void stop();

    // Replaces the endpoint and wakes the worker for an immediate poll, so
    // hitting Apply in the settings window gives an answer now rather than up
    // to a poll interval later.
    void setEndpoint(std::string baseUrl, std::string token);

    // Wakes the worker without changing anything. The Refresh button.
    void refresh();

    // Queues a print. Returns immediately; watch actionResult() for the
    // outcome. A second call before the first finishes replaces it, which the
    // UI prevents by disabling the button while one is pending.
    void submitPrint(PrintRequest request);

    // POST /print/barcodes -- one barcode label per SKU in [lower, upper].
    void submitBarcodes(int lower, int upper);

    // POST /preview -- the label this request would print, as a PNG, printing
    // nothing. Takes the same fields; copies is ignored.
    //
    // Queued separately from prints rather than sharing their slot, so asking
    // for a preview can never displace a print that was already on its way.
    void submitPreview(PrintRequest request);

    void clearAction();
    void clearPreview();

    // All thread-safe, all return copies, which is what lets the caller hold
    // one for a whole frame without blocking the worker.
    Snapshot snapshot() const;
    ActionResult actionResult() const;
    PreviewResult previewResult() const;

private:
    // One slot, not a queue: the UI submits one action at a time.
    struct PendingAction {
        enum class Kind {
            Print,
            Barcodes,
        };

        Kind kind = Kind::Print;
        PrintRequest print;
        int lower = 0;
        int upper = 0;
    };

    void run();
    void pollOnce();
    void runAction(const PendingAction& action);
    void runPreview(const PrintRequest& request);

    mutable std::mutex mutex_;
    std::condition_variable wake_;

    std::string baseUrl_;
    std::string token_;
    Snapshot snapshot_;

    ActionResult action_;
    std::optional<PendingAction> pendingAction_;

    PreviewResult preview_;
    std::optional<PrintRequest> pendingPreview_;

    std::atomic<bool> running_ { false };
    std::thread worker_;
};

}  // namespace lipgloss
