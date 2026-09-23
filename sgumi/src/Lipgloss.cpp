#include "Lipgloss.hpp"

#include "Http.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <utility>

using json = nlohmann::json;

namespace lipgloss {
namespace {

constexpr auto kPollInterval = std::chrono::seconds(1);

// What lipgloss records as having asked for a job, and what the queue table
// shows in its Source column. The kiosk sends "terminal" and the bot sends
// "discord:<user>", so jobs from here are distinguishable from both.
constexpr const char* kSource = "sgumi";

// The service returns JSON nulls for unset config -- printer_port is null when
// lipgloss.printer.port was left blank -- and json::get<std::string> throws on
// those rather than yielding "". Every read goes through here.
std::string str(const json& data, const char* key, const char* fallback = "") {
    if (!data.contains(key) || data[key].is_null()) {
        return fallback;
    }

    if (data[key].is_string()) {
        return data[key].get<std::string>();
    }

    return data[key].dump();
}

int integer(const json& data, const char* key) {
    if (!data.contains(key) || !data[key].is_number_integer()) {
        return 0;
    }

    return data[key].get<int>();
}

// lipgloss's PrintRequest treats an absent field and an empty string
// differently: missing_values() checks falsiness, so "" and null both read as
// absent, but sending null is what the Python clients do and keeps the two
// callers byte-comparable in the service log.
void putOrNull(json& out, const char* key, const std::string& value) {
    if (value.empty()) {
        out[key] = nullptr;
    } else {
        out[key] = value;
    }
}

}  // namespace

Client::Client() = default;

Client::~Client() {
    stop();
}

void Client::start() {
    if (running_.exchange(true)) {
        return;
    }

    worker_ = std::thread(&Client::run, this);
}

void Client::stop() {
    if (!running_.exchange(false)) {
        return;
    }

    wake_.notify_all();

    if (worker_.joinable()) {
        worker_.join();
    }
}

void Client::setEndpoint(std::string baseUrl, std::string token) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl_ = std::move(baseUrl);
        token_ = std::move(token);

        // The old snapshot describes a different service. Showing it beside a
        // new URL would be a lie for up to a poll interval, so drop it and let
        // the UI say "connecting" until the first poll lands.
        snapshot_ = Snapshot {};
    }

    wake_.notify_all();
}

void Client::refresh() {
    wake_.notify_all();
}

void Client::submitPrint(PrintRequest request) {
    {
        std::lock_guard<std::mutex> lock(mutex_);

        action_ = ActionResult {};
        action_.state = ActionResult::State::Pending;
        action_.kind = ActionResult::Kind::Print;

        PendingAction pending;
        pending.kind = PendingAction::Kind::Print;
        pending.print = std::move(request);
        pendingAction_ = std::move(pending);
    }

    wake_.notify_all();
}

void Client::submitBarcodes(int lower, int upper) {
    {
        std::lock_guard<std::mutex> lock(mutex_);

        action_ = ActionResult {};
        action_.state = ActionResult::State::Pending;
        action_.kind = ActionResult::Kind::Barcodes;

        PendingAction pending;
        pending.kind = PendingAction::Kind::Barcodes;
        pending.lower = lower;
        pending.upper = upper;
        pendingAction_ = std::move(pending);
    }

    wake_.notify_all();
}

void Client::submitResume() {
    {
        std::lock_guard<std::mutex> lock(mutex_);

        action_ = ActionResult {};
        action_.state = ActionResult::State::Pending;
        action_.kind = ActionResult::Kind::Resume;

        PendingAction pending;
        pending.kind = PendingAction::Kind::Resume;
        pendingAction_ = std::move(pending);
    }

    wake_.notify_all();
}

void Client::submitPreview(PrintRequest request) {
    {
        std::lock_guard<std::mutex> lock(mutex_);

        // The old PNG is kept while the new one is in flight, so refreshing a
        // preview does not blank the one already on screen. serial is what
        // tells the UI whether to re-upload, and it does not move until the
        // bytes actually change.
        preview_.state = PreviewResult::State::Pending;
        preview_.error.clear();

        pendingPreview_ = std::move(request);
    }

    wake_.notify_all();
}

void Client::clearAction() {
    std::lock_guard<std::mutex> lock(mutex_);
    action_ = ActionResult {};
}

void Client::clearPreview() {
    std::lock_guard<std::mutex> lock(mutex_);

    // serial deliberately not reset: it only ever counts up, so a UI holding
    // an old value cannot mistake a cleared preview for the one it uploaded.
    preview_.state = PreviewResult::State::Idle;
    preview_.png.clear();
    preview_.error.clear();
    preview_.serial++;
    pendingPreview_.reset();
}

PreviewResult Client::previewResult() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return preview_;
}

Snapshot Client::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return snapshot_;
}

ActionResult Client::actionResult() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return action_;
}

void Client::run() {
    while (running_.load()) {
        // Actions before the poll: someone is watching a button, whereas the
        // poll is background. Running it first also means the poll that
        // follows already reflects the job just queued.
        std::optional<PendingAction> pending;
        std::optional<PrintRequest> preview;

        {
            std::lock_guard<std::mutex> lock(mutex_);
            pending.swap(pendingAction_);
            preview.swap(pendingPreview_);
        }

        if (pending) {
            runAction(*pending);
        }

        // After the action: a print and a preview submitted in the same breath
        // should put the job on the queue first, since that is the one with a
        // printer waiting on it.
        if (preview) {
            runPreview(*preview);
        }

        pollOnce();

        std::unique_lock<std::mutex> lock(mutex_);

        // Predicated on all three, so neither a stop nor submitted work waits
        // out the remaining interval.
        wake_.wait_for(lock, kPollInterval, [this] {
            return !running_.load() ||
                   pendingAction_.has_value() ||
                   pendingPreview_.has_value();
        });
    }
}

void Client::runAction(const PendingAction& action) {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    ActionResult result;

    // Carried across from the request. The result is built fresh here, so
    // without this it would default to Print and a resume would be reported as
    // a job that never existed.
    switch (action.kind) {
    case PendingAction::Kind::Print:
        result.kind = ActionResult::Kind::Print;
        break;
    case PendingAction::Kind::Barcodes:
        result.kind = ActionResult::Kind::Barcodes;
        break;
    case PendingAction::Kind::Resume:
        result.kind = ActionResult::Kind::Resume;
        break;
    }

    if (baseUrl.empty()) {
        result.state = ActionResult::State::Failed;
        result.message = "No lipgloss URL configured.";

        std::lock_guard<std::mutex> lock(mutex_);
        action_ = std::move(result);
        return;
    }

    std::string path;
    json body;

    if (action.kind == PendingAction::Kind::Print) {
        path = "/print";

        body["style"] = action.print.style;
        putOrNull(body, "sku", action.print.sku);
        putOrNull(body, "line_1", action.print.line1);
        putOrNull(body, "line_2", action.print.line2);
        body["copies"] = std::clamp(action.print.copies, 1, kMaxCopies);
        body["source"] = kSource;
    } else if (action.kind == PendingAction::Kind::Barcodes) {
        path = "/print/barcodes";

        body["lower"] = action.lower;
        body["upper"] = action.upper;
        body["source"] = kSource;
    } else {
        // Resume takes no body at all. FastAPI is content with an empty JSON
        // object on a POST that declares no model.
        path = "/queue/resume";
        body = json::object();
    }

    const http::Response response =
        http::post(http::join(baseUrl, path.c_str()), token, body.dump());

    if (!response.transportOk) {
        result.state = ActionResult::State::Failed;
        result.message = response.error;
    } else if (response.status == 401 || response.status == 403) {
        result.state = ActionResult::State::Failed;
        result.message = "lipgloss rejected the token.";
    } else if (response.status == 422) {
        // FastAPI's validation error. The body names the offending field, and
        // that detail is far more useful than "422" -- a style needing a SKU
        // that did not get one lands here.
        result.state = ActionResult::State::Failed;
        result.message = "lipgloss refused the request: " + response.body;
    } else if (response.status != 200) {
        result.state = ActionResult::State::Failed;
        result.message =
            "lipgloss returned HTTP " + std::to_string(response.status);
    } else {
        try {
            const json data = json::parse(response.body);

            result.message = str(data, "message");
            result.queuePaused = data.value("paused", false);

            if (data.contains("job_id") && data["job_id"].is_number_integer()) {
                result.jobId = data["job_id"].get<long long>();
            }

            if (action.kind == PendingAction::Kind::Resume) {
                // Resume carries no job_id -- there is no job. A 200 is the
                // whole answer, and the message is lipgloss's own account of
                // what the queue did.
                result.state = ActionResult::State::Ok;

                if (result.message.empty()) {
                    result.message = "Queue resumed.";
                }
            } else {
                // lipgloss answers 200 with a null job_id and an explanatory
                // message when it declines a job -- an empty barcode range, or
                // a style missing a field. A null id is therefore a refusal,
                // not a success with no id.
                result.state = result.jobId >= 0
                    ? ActionResult::State::Ok
                    : ActionResult::State::Failed;

                if (result.message.empty()) {
                    result.message = result.jobId >= 0
                        ? "Queued."
                        : "lipgloss declined the job without saying why.";
                }
            }
        } catch (const json::exception& e) {
            result.state = ActionResult::State::Failed;
            result.message =
                std::string("lipgloss did not return JSON: ") + e.what();
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);
    action_ = std::move(result);
}

void Client::runPreview(const PrintRequest& request) {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    PreviewResult result;
    result.state = PreviewResult::State::Failed;

    if (baseUrl.empty()) {
        result.error = "No lipgloss URL configured.";
    } else {
        json body;
        body["style"] = request.style;
        putOrNull(body, "sku", request.sku);
        putOrNull(body, "line_1", request.line1);
        putOrNull(body, "line_2", request.line2);
        body["scale"] = kPreviewScale;

        const http::Response response =
            http::post(http::join(baseUrl, "/preview"), token, body.dump());

        if (!response.transportOk) {
            result.error = response.error;
        } else if (response.status == 401 || response.status == 403) {
            result.error = "lipgloss rejected the token.";
        } else if (response.status != 200) {
            // Not JSON-parsed: this endpoint answers with a PNG when it is
            // happy, so a failure body is whatever FastAPI felt like saying,
            // and quoting it verbatim beats guessing at its shape.
            result.error = "lipgloss returned HTTP " +
                           std::to_string(response.status) + ": " + response.body;
        } else if (response.body.empty()) {
            result.error = "lipgloss returned an empty preview.";
        } else {
            result.state = PreviewResult::State::Ready;
            result.png = response.body;
        }
    }

    std::lock_guard<std::mutex> lock(mutex_);

    // Carried over and incremented rather than restarted, so the counter is
    // monotonic for the life of the client and the UI can compare against it
    // without worrying about wraparound or reuse.
    result.serial = preview_.serial + 1;

    // A failed refresh keeps the last good PNG on screen with the error beside
    // it, which is more useful than a blank panel.
    if (result.state != PreviewResult::State::Ready) {
        result.png = preview_.png;
    }

    preview_ = std::move(result);
}

void Client::pollOnce() {
    std::string baseUrl;
    std::string token;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        baseUrl = baseUrl_;
        token = token_;
    }

    Snapshot next;

    if (baseUrl.empty()) {
        next.error = "No lipgloss URL configured.";

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = std::move(next);
        return;
    }

    // Health first, and unauthenticated. It is the call that distinguishes
    // "the service is not there" from "the service is there and does not like
    // our token", which are the two failures worth telling apart on a kiosk.
    const http::Response health = http::get(http::join(baseUrl, "/health"), "");

    if (!health.transportOk) {
        next.error = health.error;
        next.polled = std::chrono::steady_clock::now();

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = std::move(next);
        return;
    }

    if (health.status != 200) {
        next.error = "GET /health returned HTTP " + std::to_string(health.status);
        next.polled = std::chrono::steady_clock::now();

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = std::move(next);
        return;
    }

    // A body that is not JSON almost always means the URL points at something
    // that is not lipgloss -- a proxy error page, or the wrong port. Caught
    // rather than thrown so one bad poll does not take the worker down.
    try {
        const json data = json::parse(health.body);

        next.version = str(data, "version", "unknown");
        next.printerPort = str(data, "printer_port", "(unset)");
        next.model = str(data, "model", "unknown");

        if (data.contains("uptime_ms") && data["uptime_ms"].is_number()) {
            next.uptimeMs = data["uptime_ms"].get<long long>();
        }

        next.reachable = true;
    } catch (const json::exception& e) {
        next.error = std::string("GET /health did not return JSON: ") + e.what();
        next.polled = std::chrono::steady_clock::now();

        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = std::move(next);
        return;
    }

    const http::Response queue = http::get(http::join(baseUrl, "/queue"), token);

    if (!queue.transportOk) {
        // Health answered a moment ago, so this is a stall rather than an
        // outage. reachable stays true: the service is up, the queue view is
        // just empty this frame.
        next.error = queue.error;
    } else if (queue.status == 401 || queue.status == 403) {
        next.unauthorized = true;
        next.error =
            "lipgloss rejected the token. It must match lipgloss.yaml's "
            "lipgloss.token on the printer host.";
    } else if (queue.status != 200) {
        next.error = "GET /queue returned HTTP " + std::to_string(queue.status);
    } else {
        try {
            const json data = json::parse(queue.body);

            next.title = str(data, "title");
            next.description = str(data, "description");
            next.pauseReason = str(data, "pause_reason");
            next.paused = data.value("paused", false);
            next.pendingJobs = integer(data, "pending_jobs");
            next.pendingLabels = integer(data, "pending_labels");

            if (data.contains("jobs") && data["jobs"].is_array()) {
                for (const json& row : data["jobs"]) {
                    Job job;
                    job.jobId = str(row, "JOB_ID");
                    job.description = str(row, "DESCRIPTION");
                    job.labels = str(row, "LABELS");
                    job.source = str(row, "SOURCE");
                    job.state = str(row, "STATE");
                    next.jobs.push_back(std::move(job));
                }
            }
        } catch (const json::exception& e) {
            next.error = std::string("GET /queue did not return JSON: ") + e.what();
        }
    }

    next.polled = std::chrono::steady_clock::now();

    std::lock_guard<std::mutex> lock(mutex_);
    snapshot_ = std::move(next);
}

}  // namespace lipgloss
