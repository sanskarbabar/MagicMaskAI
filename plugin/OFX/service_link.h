// AI Cutout — plugin side of the local-service protocol (loopback TCP + per-user token, newline-delimited JSON).
#pragma once
#include <string>
#include <vector>

namespace acut {

// ---- tiny JSON helpers (the protocol is flat; we control both ends) ----------------------------------------
std::string jstr(const std::string& s);                                    // quoted + escaped
bool jGetString(const std::string& json, const char* key, std::string& out);
bool jGetNumber(const std::string& json, const char* key, double& out);
bool jGetBool(const std::string& json, const char* key, bool& out);
std::vector<int> jGetIntArray(const std::string& json, const char* key);

struct Reply {
    bool ok = false;
    bool transport = false;      // true if the failure was a connection problem (service not reachable)
    std::string raw;
    std::string error;
};

class ServiceLink {
public:
    // One short-lived connection per call: no shared socket state between plugin threads.
    static bool readInfo(int& port, std::string& token);
    static Reply call(const std::string& cmd, const std::string& fieldsJson = "", int timeoutMs = 4000);
    static bool running();                                           // service.json present and answers "hello"
    // Start the service (install.ini / AICUTOUT_SERVICE_CMD) and wait until it answers. err gets a human message.
    static bool ensureRunning(std::string& err, int waitMs = 45000);
};

std::string stateDir();                    // %LOCALAPPDATA%\AICutout
std::string cacheRoot();                   // AICUTOUT_CACHE or <stateDir>\cache

void logf(const char* fmt, ...);           // %LOCALAPPDATA%\AICutout\logs\plugin.log (rotated at 1 MB)

}  // namespace acut
