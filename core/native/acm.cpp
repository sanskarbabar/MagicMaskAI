#include "acm.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace acut {

static uint32_t g_crcTable[256];
static bool g_crcInit = false;

static void initCrc() {
    if (g_crcInit) return;
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (int k = 0; k < 8; ++k) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        g_crcTable[i] = c;
    }
    g_crcInit = true;
}

uint32_t crc32(const uint8_t* p, size_t n) {
    initCrc();
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) c = g_crcTable[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

static uint32_t rd32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | (uint32_t(p[3]) << 24); }

bool decodeFrame(const uint8_t* d, size_t len, Matte& out) {
    if (len < 24 || std::memcmp(d, "ACM1", 4) != 0) return false;
    uint32_t w = rd32(d + 4), h = rd32(d + 8), crc = rd32(d + 12);
    uint8_t flags = d[16], conf = d[17];
    uint32_t plen = rd32(d + 20);
    if (w == 0 || h == 0 || w > 32768 || h > 32768) return false;
    if (len < 24 + size_t(plen) || plen % 3 != 0) return false;
    const uint8_t* pay = d + 24;
    if (crc32(pay, plen) != crc) return false;
    size_t total = size_t(w) * h, pos = 0;
    std::vector<uint8_t> a(total);
    for (size_t i = 0; i < plen; i += 3) {
        size_t run = pay[i + 1] | (size_t(pay[i + 2]) << 8);
        if (pos + run > total) return false;
        std::memset(a.data() + pos, pay[i], run);
        pos += run;
    }
    if (pos != total) return false;
    out.w = int(w); out.h = int(h); out.flags = flags; out.confidence = conf / 255.f;
    out.a.swap(a);
    return true;
}

bool readFrameFile(const std::string& path, Matte& out) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::vector<uint8_t> buf;
    std::fseek(f, 0, SEEK_END);
    long n = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    if (n <= 0 || n > (256L << 20)) { std::fclose(f); return false; }
    buf.resize(size_t(n));
    size_t got = std::fread(buf.data(), 1, buf.size(), f);
    std::fclose(f);
    return got == buf.size() && decodeFrame(buf.data(), buf.size(), out);
}

static std::map<std::string, std::string> readKV(const std::string& path) {
    std::map<std::string, std::string> kv;
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return kv;
    std::string line;
    int c;
    while ((c = std::fgetc(f)) != EOF) {
        if (c == '\n') {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            size_t eq = line.find('=');
            if (eq != std::string::npos) kv[line.substr(0, eq)] = line.substr(eq + 1);
            line.clear();
        } else line.push_back(char(c));
    }
    if (!line.empty()) { size_t eq = line.find('='); if (eq != std::string::npos) kv[line.substr(0, eq)] = line.substr(eq + 1); }
    std::fclose(f);
    return kv;
}

MatteMeta readMeta(const std::string& dir) {
    MatteMeta m;
    auto kv = readKV(dir + "/meta.ini");
    if (kv.empty()) return m;
    auto num = [&](const char* k) { auto it = kv.find(k); return it == kv.end() ? 0.0 : std::atof(it->second.c_str()); };
    m.width = int(num("width")); m.height = int(num("height")); m.frames = int(num("frames")); m.fps = num("fps");
    m.source = kv["source"]; m.model = kv["model"]; m.mode = kv["mode"]; m.tier = kv["tier"];
    m.ok = m.width > 0 && m.height > 0 && m.frames > 0;
    return m;
}

std::string readActiveSet(const std::string& root) {
    FILE* f = std::fopen((root + "/_active.txt").c_str(), "rb");
    if (!f) return "";
    char buf[512]; size_t n = std::fread(buf, 1, sizeof(buf) - 1, f); std::fclose(f);
    std::string s(buf, n);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) s.pop_back();
    return s;
}

std::string defaultCacheRoot() {
    if (const char* e = std::getenv("AICUTOUT_CACHE")) return e;
    const char* base = std::getenv("LOCALAPPDATA");
    if (!base) base = std::getenv("HOME");
    return std::string(base ? base : ".") + "/AICutout/cache";
}

std::string framePath(const std::string& dir, int idx) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "/masks/%06d.acm", idx);
    return dir + buf;
}

}  // namespace acut
