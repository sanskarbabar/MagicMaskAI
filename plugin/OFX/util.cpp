#include "util.h"

#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <sys/stat.h>

namespace acut {

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
    std::fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
    va_list ap; va_start(ap, fmt); std::vfprintf(f, fmt, ap); va_end(ap);
    std::fputc('\n', f);
    std::fclose(f);
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

bool launchApp(std::string& err) {
    std::string cmd = envOr("AICUTOUT_APP_CMD", "");
    if (cmd.empty()) {
        std::string ini = envOr("PROGRAMDATA", "C:\\ProgramData") + "\\AICutout\\install.ini";
        readIniValue(ini, "app", cmd);
    }
    if (cmd.empty()) {
        err = "AI Cutout is not installed. Run the AI Cutout installer, then try again.";
        return false;
    }
    logf("launching app: %s", cmd.c_str());
    STARTUPINFOA si{}; si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::string mutableCmd = cmd;
    if (!CreateProcessA(nullptr, mutableCmd.data(), nullptr, nullptr, FALSE, DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                        nullptr, nullptr, &si, &pi)) {
        err = "Could not start AI Cutout (Windows error " + std::to_string(GetLastError()) + ").";
        return false;
    }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return true;
}

}  // namespace acut
