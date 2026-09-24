#include "Http.hpp"

#include <curl/curl.h>

namespace http {
namespace {

// Long enough that a busy Niimbot driver does not read as "down", short enough
// that the UI is not stuck on a stale snapshot for a whole poll cycle.
constexpr long kTimeoutSeconds = 5;

size_t writeToString(char* data, size_t size, size_t count, void* userp) {
    const size_t total = size * count;
    static_cast<std::string*>(userp)->append(data, total);
    return total;
}

// Everything the verbs share. A fresh handle per call: these happen at most
// once a second on a worker thread, so handle reuse would buy nothing
// measurable and cost the rule that nothing here is shared between threads.
//
// verb is nullptr for the two libcurl already has a flag for, GET and POST;
// anything else is spelled out with CUSTOMREQUEST.
Response perform(
    const std::string& url,
    const std::string& token,
    const std::string* body,
    const char* verb = nullptr)
{
    Response response;

    CURL* curl = curl_easy_init();

    if (!curl) {
        response.error = "curl_easy_init failed";
        return response;
    }

    curl_slist* headers = nullptr;

    if (!token.empty()) {
        // Every endpoint except lipgloss's /health and claws's /health checks
        // a bearer token; sending it to those two as well is harmless.
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

    if (body) {
        headers = curl_slist_append(headers, "Content-Type: application/json");

        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body->c_str());

        // Explicit length: a body is not guaranteed to be free of embedded
        // nulls, and libcurl would otherwise strlen it.
        curl_easy_setopt(
            curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body->size()));
    }

    if (verb) {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, verb);
    }

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

}  // namespace

void globalInit() {
    curl_global_init(CURL_GLOBAL_DEFAULT);
}

void globalCleanup() {
    curl_global_cleanup();
}

Response get(const std::string& url, const std::string& token) {
    return perform(url, token, nullptr);
}

Response post(
    const std::string& url,
    const std::string& token,
    const std::string& body)
{
    return perform(url, token, &body);
}

Response del(const std::string& url, const std::string& token) {
    return perform(url, token, nullptr, "DELETE");
}

std::string join(const std::string& baseUrl, const char* path) {
    std::string base = baseUrl;

    while (!base.empty() && base.back() == '/') {
        base.pop_back();
    }

    return base + path;
}

}  // namespace http
