// AI Cutout — matte cache reader (format defined in core/cache/matte_store.py; keep in sync).
#pragma once
#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace acut {

struct Matte {
    int w = 0, h = 0;
    float confidence = 0.f;
    int flags = 0;               // bit0 manual keyframe, bit1 low confidence
    std::vector<uint8_t> a;      // w*h, row 0 = TOP of the frame
    bool valid() const { return w > 0 && h > 0 && a.size() == size_t(w) * h; }
};

uint32_t crc32(const uint8_t* p, size_t n);

// Decode one .acm frame file image. Returns false on any structural / CRC problem.
bool decodeFrame(const uint8_t* data, size_t len, Matte& out);
bool readFrameFile(const std::string& path, Matte& out);

struct MatteMeta {
    bool ok = false;
    int width = 0, height = 0, frames = 0;
    double fps = 0;
    std::string source, model, mode, tier;
};
MatteMeta readMeta(const std::string& setDir);

std::string readActiveSet(const std::string& cacheRoot);            // contents of _active.txt or ""
std::string defaultCacheRoot();                                     // AICUTOUT_CACHE or %LOCALAPPDATA%\AICutout\cache
std::string framePath(const std::string& setDir, int idx);

}  // namespace acut
