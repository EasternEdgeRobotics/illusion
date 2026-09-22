#include "Lipgloss.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <utility>

using json = nlohmann::json;

namespace lipgloss {
namespace {

// Long enough that a busy Niimbot driver does not read as "down", short enough
// that the UI is not stuck on a stale snapshot for a whole poll cycle.
constexpr long kTimeoutSeconds = 5;

constexpr auto kPollInterval = std::chrono::seconds(1);

size_t writeToString(char* data, size_t size, size_t count, void* userp) {
    const size_t total = size * count;
    static_cast<std::string*>(userp)->append(data, total);
    return total;
}

struct Response {
    bool transportOk = false;
    long status = 0;
    std::string body;
    std::string error;
};

// One GET. A fresh handle per call: these happen once a second on a worker
// thread, so handle reuse would buy nothing measurable and cost the rule that
// nothing here is shared between threads.
Response get(const std::string& url, const std::string& token) {
    Response response;

    CURL* curl = curl_easy_init();

    if (!curl) {
        response.error = "curl_easy_init failed";
        return response;
    }

    curl_slist* headers = nullptr;

    if (!token.empty()) {
        // lipgloss checks a bearer token on everything except /health; see
        // require_token in packages/lipgloss/src/lipgloss/service.py.
        const std::string authorization = "Authorization: Bearer " + token;
        headers = curl_slist_append(headers, authorization.c_str());
    }

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeToString);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response.body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, kTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

    if (headers) {
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    }

    const CURLcode result = curl_easy_perform(curl);

    if (result == CURLE_OK) {
        response.transportOk = true;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);
    } else {
        response.error = curl_easy_strerror(result);
    }

    if (headers) {
        curl_slist_free_all(headers);
    }

    curl_easy_cleanup(curl);
    return response;
}

// Trailing slashes would produce "http://host:8081//health", which works but
// looks like a bug in every log line it appears in.
std::string join(const std::string& baseUrl, const char* path) {
    std::string base = baseUrl;

    while (!base.empty() && base.back() == '/') {
        base.pop_back();
    }

    return base + path;
}

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

}  // namespace

Client::Client() {
    // Global init is documented as not thread-safe and as being called
    // implicitly by the first curl_easy_init() if nobody does it first. Doing
    // it here means it happens on the main thread during construction, before
    // the worker exists, rather than racing inside the first poll.
    curl_global_init(CURL_GLOBAL_DEFAULT);
}

Client::~Client() {
    stop();
    curl_global_cleanup();
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

Snapshot Client::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return snapshot_;
}

void Client::run() {
    while (running_.load()) {
        pollOnce();

        std::unique_lock<std::mutex> lock(mutex_);

        // Predicate on running_ so stop() is not waited out: the notify in
        // stop() lands here, the predicate is already false, and the worker
        // leaves immediately rather than after the remaining interval.
        wake_.wait_for(lock, kPollInterval, [this] {
            return !running_.load();
        });
    }
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
    const Response health = get(join(baseUrl, "/health"), "");

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

    const Response queue = get(join(baseUrl, "/queue"), token);

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
