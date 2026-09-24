#pragma once

#include <string>

// The bit of libcurl both service clients need, and nothing more.

namespace http {

struct Response {
    // The request reached a server and came back. Says nothing about what the
    // server thought of it -- check status for that.
    bool transportOk = false;

    long status = 0;
    std::string body;

    // Only set when transportOk is false.
    std::string error;
};

// Called once from main, around everything else.
void globalInit();
void globalCleanup();

// All three send Authorization: Bearer <token> when token is non-empty, and
// all three block the calling thread, callers are expected to be on a worker.
Response get(const std::string& url, const std::string& token);

// Content-Type: application/json, with body sent verbatim.
Response post(
    const std::string& url,
    const std::string& token,
    const std::string& body);

// No body: the only DELETE either service has is lipgloss's /queue/{id}, which
// puts the whole request in the path.
Response del(const std::string& url, const std::string& token);

// Joins a base URL and an absolute path without doubling the slash --
// "http://host:8081//health" works but looks like a bug in every log line it
// appears in.
std::string join(const std::string& baseUrl, const char* path);

}  // namespace http
