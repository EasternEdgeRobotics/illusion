#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// Client for the lipgloss print service.
//
// Mirrors LipglossClient in packages/illusion-core/src/illusion_core/clients.py,
// which is the authority on the wire format -- when an endpoint changes there,
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

    // Thread-safe. Returns a copy, which is what lets the caller hold it for a
    // whole frame without blocking the worker.
    Snapshot snapshot() const;

private:
    void run();
    void pollOnce();

    mutable std::mutex mutex_;
    std::condition_variable wake_;

    std::string baseUrl_;
    std::string token_;
    Snapshot snapshot_;

    std::atomic<bool> running_ { false };
    std::thread worker_;
};

}  // namespace lipgloss
