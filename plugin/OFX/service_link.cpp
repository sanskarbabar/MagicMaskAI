#include "service_link.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <sys/stat.h>

namespace acut {

// ---------------------------------------------------------------------------------------------------- paths
static std::string envOr(const char* name, const std::string& fallback) {
    char buf[4096];
    DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : fallback;
}

std::string stateDir() {
    std::string s = envOr("AICUTOUT_STATE", "");
    if (!s.empty()) return s;
    return envOr("LOCALAPPDATA", ".") + "\\AICutout";
}

std::string cacheRoot() {
    std::string c = envOr("AICUTOUT_CACHE", "");
    return c.empty() ? stateDir() + "\\cache" : c;
}

// ---------------------------------------------------------------------------------------------------- log
void logf(const char* fmt, ...) {
    static std::mutex m;
    std::lock_guard<std::mutex> lk(m);
    std::string dir = stateDir() + "\\logs";
    CreateDirectoryA(stateDir().c_str(), nullptr);
    CreateDirectoryA(dir.c_str(), nullptr);
    std::string path = dir + "\\plugin.log";
    struct _stat64 st;
    if (_stat64(path.c_str(), &st) == 0 && st.st_size > (1 << 20)) {
        std::string old = path + ".1";
        DeleteFileA(old.c_str());
        MoveFileA(path.c_str(), old.c_str());
    }
    FILE* f = std::fopen(path.c_str(), "ab");
    if (!f) return;
    SYSTEMTIME t; GetLocalTime(&t);
    std::fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d [%lu] ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond,
                 GetCurrentThreadId());
    va_list ap; va_start(ap, fmt); std::vfprintf(f, fmt, ap); va_end(ap);
    std::fputc('\n', f);
    std::fclose(f);
}

// ---------------------------------------------------------------------------------------------------- json
std::string jstr(const std::string& s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof(b), "\\u%04x", c); o += b; }
                else o += char(c);
        }
    }
    return o + "\"";
}

static size_t findKey(const std::string& j, const char* key) {
    std::string k = std::string("\"") + key + "\"";
    size_t p = 0;
    while ((p = j.find(k, p)) != std::string::npos) {
        size_t q = p + k.size();
        while (q < j.size() && (j[q] == ' ' || j[q] == '\t')) ++q;
        if (q < j.size() && j[q] == ':') { ++q; while (q < j.size() && j[q] == ' ') ++q; return q; }
        p += k.size();
    }
    return std::string::npos;
}

bool jGetString(const std::string& j, const char* key, std::string& out) {
    size_t p = findKey(j, key);
    if (p == std::string::npos || p >= j.size() || j[p] != '"') return false;
    ++p; out.clear();
    while (p < j.size() && j[p] != '"') {
        if (j[p] == '\\' && p + 1 < j.size()) {
            char c = j[++p];
            switch (c) {
                case 'n': out += '\n'; break;
                case 't': out += '\t'; break;
                case 'r': out += '\r'; break;
                case 'u': {
                    if (p + 4 < j.size()) {
                        unsigned v = std::strtoul(j.substr(p + 1, 4).c_str(), nullptr, 16);
                        if (v < 0x80) out += char(v);
                        else if (v < 0x800) { out += char(0xC0 | (v >> 6)); out += char(0x80 | (v & 0x3F)); }
                        else { out += char(0xE0 | (v >> 12)); out += char(0x80 | ((v >> 6) & 0x3F)); out += char(0x80 | (v & 0x3F)); }
                        p += 4;
                    }
                    break;
                }
                default: out += c;
            }
        } else out += j[p];
        ++p;
    }
    return true;
}

bool jGetNumber(const std::string& j, const char* key, double& out) {
    size_t p = findKey(j, key);
    if (p == std::string::npos) return false;
    if (j.compare(p, 4, "null") == 0) return false;
    char* end = nullptr;
    double v = std::strtod(j.c_str() + p, &end);
    if (end == j.c_str() + p) return false;
    out = v;
    return true;
}

bool jGetBool(const std::string& j, const char* key, bool& out) {
    size_t p = findKey(j, key);
    if (p == std::string::npos) return false;
    if (j.compare(p, 4, "true") == 0) { out = true; return true; }
    if (j.compare(p, 5, "false") == 0) { out = false; return true; }
    return false;
}

std::vector<int> jGetIntArray(const std::string& j, const char* key) {
    std::vector<int> v;
    size_t p = findKey(j, key);
    if (p == std::string::npos || p >= j.size() || j[p] != '[') return v;
    ++p;
    while (p < j.size() && j[p] != ']') {
        char* end = nullptr;
        long n = std::strtol(j.c_str() + p, &end, 10);
        if (end == j.c_str() + p) { ++p; continue; }
        v.push_back(int(n));
        p = size_t(end - j.c_str());
    }
    return v;
}

// ---------------------------------------------------------------------------------------------------- link
static void wsInit() {
    static std::once_flag once;
    std::call_once(once, [] { WSADATA d; WSAStartup(MAKEWORD(2, 2), &d); });
}

bool ServiceLink::readInfo(int& port, std::string& token) {
    std::string path = stateDir() + "\\service.json";
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[1024]; size_t n = std::fread(buf, 1, sizeof(buf) - 1, f); std::fclose(f);
    std::string j(buf, n);
    double p;
    if (!jGetNumber(j, "port", p) || !jGetString(j, "token", token)) return false;
    port = int(p);
    return port > 0;
}

Reply ServiceLink::call(const std::string& cmd, const std::string& fields, int timeoutMs) {
    Reply r;
    wsInit();
    int port; std::string token;
    if (!readInfo(port, token)) { r.error = "The AI Cutout service is not running."; r.transport = true; return r; }
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) { r.error = "Could not create a socket."; r.transport = true; return r; }
    DWORD to = DWORD(timeoutMs);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&to), sizeof(to));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&to), sizeof(to));
    sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(u_short(port)); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(s, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0) {
        closesocket(s); r.error = "The AI Cutout service is not reachable."; r.transport = true; return r;
    }
    std::string req = "{\"token\":" + jstr(token) + ",\"id\":1,\"cmd\":" + jstr(cmd) + (fields.empty() ? "" : "," + fields) + "}\n";
    size_t sent = 0;
    while (sent < req.size()) {
        int n = send(s, req.data() + sent, int(req.size() - sent), 0);
        if (n <= 0) { closesocket(s); r.error = "Lost connection to the AI Cutout service."; r.transport = true; return r; }
        sent += size_t(n);
    }
    std::string line;
    char buf[8192];
    for (;;) {
        int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) break;
        line.append(buf, size_t(n));
        if (line.find('\n') != std::string::npos) break;
        if (line.size() > (64u << 20)) break;
    }
    closesocket(s);
    if (line.empty()) { r.error = "The AI Cutout service did not answer in time."; r.transport = true; return r; }
    r.raw = line;
    bool ok = false;
    jGetBool(line, "ok", ok);
    r.ok = ok;
    if (!ok && !jGetString(line, "error", r.error)) r.error = "Unknown service error.";
    return r;
}

bool ServiceLink::running() {
    Reply r = call("hello", "", 1500);
    return r.ok;
}

static bool readIniValue(const std::string& path, const char* key, std::string& out) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[4096]; size_t n = std::fread(buf, 1, sizeof(buf) - 1, f); std::fclose(f);
    std::string s(buf, n), k = std::string(key) + "=";
    size_t p = 0;
    while (p < s.size()) {
        size_t e = s.find('\n', p);
        std::string line = s.substr(p, e == std::string::npos ? std::string::npos : e - p);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.compare(0, k.size(), k) == 0) { out = line.substr(k.size()); return true; }
        if (e == std::string::npos) break;
        p = e + 1;
    }
    return false;
}

bool ServiceLink::ensureRunning(std::string& err, int waitMs) {
    if (running()) return true;
    std::string cmd = envOr("AICUTOUT_SERVICE_CMD", "");
    if (cmd.empty()) {
        std::string ini = envOr("PROGRAMDATA", "C:\\ProgramData") + "\\AICutout\\install.ini";
        if (!readIniValue(ini, "service", cmd)) readIniValue(stateDir() + "\\install.ini", "service", cmd);
    }
    if (cmd.empty()) {
        err = "AI Cutout service is not installed. Run the AI Cutout installer (or set AICUTOUT_SERVICE_CMD).";
        return false;
    }
    logf("launching service: %s", cmd.c_str());
    STARTUPINFOA si{}; si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::string mutableCmd = cmd;
    if (!CreateProcessA(nullptr, mutableCmd.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                        nullptr, nullptr, &si, &pi)) {
        err = "Could not start the AI Cutout service (error " + std::to_string(GetLastError()) + ").";
        return false;
    }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    for (int waited = 0; waited < waitMs; waited += 500) {
        Sleep(500);
        if (running()) return true;
    }
    err = "The AI Cutout service did not start. See " + stateDir() + "\\logs\\service.log";
    return false;
}

}  // namespace acut
