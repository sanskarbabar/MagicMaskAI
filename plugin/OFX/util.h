// AI Cutout plugin helpers: state/cache paths, logging, and starting the app. No sockets, no threads.
#pragma once
#include <string>

namespace acut {

std::string stateDir();                    // %LOCALAPPDATA%\AICutout   (AICUTOUT_STATE overrides)
std::string cacheRoot();                   // AICUTOUT_CACHE or <stateDir>\cache
void logf(const char* fmt, ...);           // <stateDir>\logs\plugin.log (rotated at 1 MB)

// Start the AI Cutout app (command from AICUTOUT_APP_CMD, else "app=" in %PROGRAMDATA%\AICutout\install.ini).
bool launchApp(std::string& err);

}  // namespace acut
