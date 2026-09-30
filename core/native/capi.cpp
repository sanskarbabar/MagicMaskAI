// Plain C API over the native core so Python (ctypes) can use the exact same code as the OFX plugin:
// used by the "Render" export and by the parity tests.
#include <cstring>

#include "acm.h"
#include "edge.h"

#if defined(_WIN32)
#define ACUT_API extern "C" __declspec(dllexport)
#else
#define ACUT_API extern "C"
#endif

struct AcutParams {
    float edgeShift, feather, smooth, refine, decontaminate, spill, cleanBlack, cleanWhite;
    int quality, mode, premultiply, checkerSize, bgKind;
    float bgColor[3];
    float overlayColor[3];
    float overlayOpacity;
    float renderScale;
};

ACUT_API int acut_version() { return 1; }

// Decode a .acm image. out must hold cap bytes. Returns 0 on success.
ACUT_API int acut_decode_frame(const uint8_t* data, size_t len, uint8_t* out, size_t cap, int* w, int* h,
                               float* conf, int* flags) {
    acut::Matte m;
    if (!acut::decodeFrame(data, len, m)) return 1;
    if (m.a.size() > cap) return 2;
    std::memcpy(out, m.a.data(), m.a.size());
    *w = m.w; *h = m.h; *conf = m.confidence; *flags = m.flags;
    return 0;
}

ACUT_API uint32_t acut_crc32(const uint8_t* p, size_t n) { return acut::crc32(p, n); }

// src/dst: contiguous top-down float RGBA (w*h*4). matte: top-down uint8 (mw*mh). Returns 0 on success.
ACUT_API int acut_render(const float* srcRGBA, float* dstRGBA, int w, int h, const uint8_t* matte, int mw, int mh,
                         const AcutParams* p) {
    if (!srcRGBA || !dstRGBA || w <= 0 || h <= 0 || !p) return 1;
    acut::RGBAImage src{const_cast<float*>(srcRGBA), w, h, ptrdiff_t(w) * 4};
    acut::RGBAImage dst{dstRGBA, w, h, ptrdiff_t(w) * 4};
    acut::RenderParams rp;
    rp.edge.edgeShift = p->edgeShift; rp.edge.feather = p->feather; rp.edge.smooth = p->smooth;
    rp.edge.refine = p->refine; rp.edge.decontaminate = p->decontaminate; rp.edge.spill = p->spill;
    rp.edge.cleanBlack = p->cleanBlack; rp.edge.cleanWhite = p->cleanWhite; rp.edge.quality = p->quality;
    rp.mode = acut::OutputMode(p->mode);
    rp.premultiply = p->premultiply != 0;
    rp.matteFlipY = false;               // contiguous top-down buffers
    rp.checkerSize = p->checkerSize > 0 ? p->checkerSize : 32;
    rp.bgKind = p->bgKind == 1 ? acut::kBgSolid : acut::kBgChecker;
    for (int i = 0; i < 3; ++i) { rp.bgColor[i] = p->bgColor[i]; rp.overlayColor[i] = p->overlayColor[i]; }
    rp.overlayOpacity = p->overlayOpacity;
    rp.renderScale = p->renderScale > 0 ? p->renderScale : 1.f;
    acut::Matte m;
    const acut::Matte* mp = nullptr;
    if (matte && mw > 0 && mh > 0) { m.w = mw; m.h = mh; m.a.assign(matte, matte + size_t(mw) * mh); mp = &m; }
    acut::render(src, mp, rp, dst);
    return 0;
}
