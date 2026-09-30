// AI Cutout — full-resolution edge pipeline + output composition (CPU, multi-threaded).
//
//   raw matte -> upsample -> noise cleanup (levels) -> morphology (edge shift) -> guided edge refinement
//             -> shape smoothing -> feather -> decontamination / spill -> final alpha -> output mode
//
// Buffers are interleaved float RGBA in *memory order*; `stride` is in floats and may be negative
// (OFX images are bottom-up). Matte rows are top-down, so matteFlipY must be true for OFX buffers.
#pragma once
#include <cstddef>
#include <cstdint>

#include "acm.h"

namespace acut {

struct RGBAImage {
    float* data = nullptr;        // pointer to memory row 0, pixel 0
    int w = 0, h = 0;
    ptrdiff_t stride = 0;         // floats between rows (negative allowed)
    float* px(int x, int y) const { return data + ptrdiff_t(y) * stride + ptrdiff_t(x) * 4; }
};

struct EdgeParams {
    float edgeShift = 0.f;        // px; + grows the matte, - shrinks it
    float feather = 0.f;          // px; gaussian sigma of the final soft edge
    float smooth = 0.f;           // px; shape smoothing that keeps the edge width (removes jaggies)
    float refine = 0.5f;          // 0..1 guided-filter edge snapping to the image
    float decontaminate = 0.f;    // 0..1 foreground colour estimation at the edge
    float spill = 0.f;            // 0..1 spill suppression
    float cleanBlack = 0.03f;     // alpha cleanup: values below become 0
    float cleanWhite = 0.97f;     // values above become 1
    int quality = 1;              // 0 draft, 1 balanced, 2 high
};

enum OutputMode { kOriginal = 0, kMask, kAlpha, kCutout, kComposite, kOverlay, kCheckerboard };
enum BgKind { kBgChecker = 0, kBgSolid, kBgClip };

struct RenderParams {
    EdgeParams edge;
    OutputMode mode = kCutout;
    bool premultiply = false;
    bool matteFlipY = true;
    int bgKind = kBgChecker;
    float bgColor[3] = {0.f, 0.6f, 0.f};
    float overlayColor[3] = {1.f, 0.1f, 0.1f};
    float overlayOpacity = 0.55f;
    int checkerSize = 32;
    const RGBAImage* bgImage = nullptr;   // when bgKind == kBgClip
    float renderScale = 1.f;              // px params are multiplied by this (proxy playback)
};

// Render `src` with `matte` (may be null => pass-through) into `dst` (same size as src).
void render(const RGBAImage& src, const Matte* matte, const RenderParams& rp, RGBAImage& dst);

// Exposed for tests: final alpha only (w*h floats, memory order rows top->bottom of `src` buffer).
void computeAlpha(const RGBAImage& src, const Matte& matte, const EdgeParams& ep, bool flipY, float* outAlpha);

}  // namespace acut
