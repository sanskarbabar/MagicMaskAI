#include "edge.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <functional>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <windows.h>
#endif

namespace acut {

// ------------------------------------------------------------------------------------------- parallel
// parallelFor(n, f): runs f(begin, end) over [0, n) split across cores and returns when all chunks are done.
// The pipeline calls it hundreds of times per frame, so threads must not be created per call: on Windows the
// system thread pool is used (persistent workers, and safe when the plugin DLL is unloaded because every call
// blocks until its chunks have finished). Other platforms fall back to std::thread.
#if defined(_WIN32)
struct PfChunk { const std::function<void(int, int)>* f; int a, b; std::atomic<int>* remaining; HANDLE ev; };
static VOID CALLBACK pfCallback(PTP_CALLBACK_INSTANCE, PVOID p) {
    auto* c = static_cast<PfChunk*>(p);
    (*c->f)(c->a, c->b);
    if (c->remaining->fetch_sub(1) == 1) SetEvent(c->ev);
}
static unsigned coreCount() {
    static unsigned n = [] { SYSTEM_INFO si; GetSystemInfo(&si); return si.dwNumberOfProcessors ? si.dwNumberOfProcessors : 4u; }();
    return n;
}
template <class F>
static void parallelFor(int n, F&& f) {
    if (n <= 0) return;
    int nt = int(std::min<unsigned>(coreCount(), 16));
    if (n < 64 || nt <= 1) { f(0, n); return; }
    nt = std::min(nt, n);
    std::function<void(int, int)> fn = [&f](int a, int b) { f(a, b); };
    std::atomic<int> remaining(nt);
    HANDLE ev = CreateEventA(nullptr, TRUE, FALSE, nullptr);
    if (!ev) { f(0, n); return; }
    std::vector<PfChunk> chunks;
    chunks.resize(size_t(nt));
    for (int t = 0; t < nt; ++t)
        chunks[size_t(t)] = {&fn, int(int64_t(n) * t / nt), int(int64_t(n) * (t + 1) / nt), &remaining, ev};
    for (int t = 1; t < nt; ++t)
        if (!TrySubmitThreadpoolCallback(pfCallback, &chunks[size_t(t)], nullptr)) pfCallback(nullptr, &chunks[size_t(t)]);
    pfCallback(nullptr, &chunks[0]);                       // the calling thread does chunk 0 itself
    WaitForSingleObject(ev, INFINITE);
    CloseHandle(ev);
}
#else
template <class F>
static void parallelFor(int n, F&& f) {
    if (n <= 0) return;
    unsigned hw = std::thread::hardware_concurrency();
    int nt = int(std::min<unsigned>(hw ? hw : 4, 12));
    if (n < 64 || nt <= 1) { f(0, n); return; }
    nt = std::min(nt, n);
    std::vector<std::thread> ts;
    ts.reserve(size_t(nt));
    for (int t = 0; t < nt; ++t) {
        int a = int(int64_t(n) * t / nt), b = int(int64_t(n) * (t + 1) / nt);
        ts.emplace_back([&f, a, b] { f(a, b); });
    }
    for (auto& th : ts) th.join();
}
#endif

using Plane = std::vector<float>;

static inline float clamp01(float v) { return v < 0.f ? 0.f : (v > 1.f ? 1.f : v); }

// ------------------------------------------------------------------------------------------- box / gauss
// Separable running-sum box blur with edge clamping. `tmp` must have the same size as in/out.
static void boxBlur(const float* in, float* out, float* tmp, int w, int h, int r) {
    if (r <= 0) { if (in != out) std::memcpy(out, in, sizeof(float) * size_t(w) * h); return; }
    const float inv = 1.f / float(2 * r + 1);
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const float* row = in + size_t(y) * w;
            float* o = tmp + size_t(y) * w;
            double acc = 0;
            for (int i = -r; i <= r; ++i) acc += row[std::min(std::max(i, 0), w - 1)];
            o[0] = float(acc * inv);
            for (int x = 1; x < w; ++x) {
                acc += row[std::min(x + r, w - 1)] - row[std::max(x - r - 1, 0)];
                o[x] = float(acc * inv);
            }
        }
    });
    parallelFor(w, [&](int c0, int c1) {
        std::vector<double> acc(size_t(c1 - c0), 0.0);
        for (int i = -r; i <= r; ++i) {
            const float* row = tmp + size_t(std::min(std::max(i, 0), h - 1)) * w;
            for (int c = c0; c < c1; ++c) acc[size_t(c - c0)] += row[c];
        }
        for (int c = c0; c < c1; ++c) out[size_t(c)] = float(acc[size_t(c - c0)] * inv);
        for (int y = 1; y < h; ++y) {
            const float* add = tmp + size_t(std::min(y + r, h - 1)) * w;
            const float* sub = tmp + size_t(std::max(y - r - 1, 0)) * w;
            float* o = out + size_t(y) * w;
            for (int c = c0; c < c1; ++c) {
                acc[size_t(c - c0)] += add[c] - sub[c];
                o[c] = float(acc[size_t(c - c0)] * inv);
            }
        }
    });
}

// Gaussian approximated by three box blurs.
static void gaussBlur(Plane& p, Plane& tmp, int w, int h, float sigma) {
    if (sigma < 0.3f) return;
    const int n = 3;
    float wIdeal = std::sqrt(12.f * sigma * sigma / n + 1.f);
    int wl = int(std::floor(wIdeal)); if (wl % 2 == 0) --wl; if (wl < 1) wl = 1;
    int wu = wl + 2;
    float mIdeal = (12.f * sigma * sigma - n * wl * wl - 4.f * n * wl - 3.f * n) / (-4.f * wl - 4.f);
    int m = int(std::round(mIdeal));
    Plane out(p.size());
    for (int i = 0; i < n; ++i) {
        int size = i < m ? wl : wu;
        boxBlur(p.data(), out.data(), tmp.data(), w, h, (size - 1) / 2);
        p.swap(out);
    }
}

// ------------------------------------------------------------------------------------------- morphology
static void maxSquare(const Plane& in, Plane& out, Plane& tmp, int w, int h, int r) {
    if (r <= 0) { out = in; return; }
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const float* row = &in[size_t(y) * w];
            float* o = &tmp[size_t(y) * w];
            for (int x = 0; x < w; ++x) {
                float m = row[x];
                int a = std::max(0, x - r), b = std::min(w - 1, x + r);
                for (int i = a; i <= b; ++i) m = std::max(m, row[i]);
                o[x] = m;
            }
        }
    });
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            float* o = &out[size_t(y) * w];
            int a = std::max(0, y - r), b = std::min(h - 1, y + r);
            std::memcpy(o, &tmp[size_t(a) * w], sizeof(float) * w);
            for (int j = a + 1; j <= b; ++j) {
                const float* row = &tmp[size_t(j) * w];
                for (int x = 0; x < w; ++x) o[x] = std::max(o[x], row[x]);
            }
        }
    });
}

static void maxCross(const Plane& in, Plane& out, int w, int h) {
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const float* c = &in[size_t(y) * w];
            const float* u = &in[size_t(std::max(y - 1, 0)) * w];
            const float* d = &in[size_t(std::min(y + 1, h - 1)) * w];
            float* o = &out[size_t(y) * w];
            for (int x = 0; x < w; ++x) {
                float m = std::max(c[x], std::max(u[x], d[x]));
                m = std::max(m, c[std::max(x - 1, 0)]);
                m = std::max(m, c[std::min(x + 1, w - 1)]);
                o[x] = m;
            }
        }
    });
}

// Octagonal (disk-like) grayscale dilation of radius R = square(a) (+) diamond(b).
static void dilateDisk(Plane& p, int w, int h, float R) {
    int Ri = int(std::lround(std::min(R, 120.f)));
    if (Ri <= 0) return;
    int a = int(std::lround(0.4142f * Ri)), b = Ri - a;
    Plane tmp(p.size()), out(p.size());
    maxSquare(p, out, tmp, w, h, a);
    p.swap(out);
    for (int i = 0; i < b; ++i) { maxCross(p, out, w, h); p.swap(out); }
}

static void shiftEdge(Plane& alpha, int w, int h, float shiftPx) {
    if (std::fabs(shiftPx) < 0.25f) return;
    if (shiftPx > 0) dilateDisk(alpha, w, h, shiftPx);
    else {
        for (auto& v : alpha) v = 1.f - v;
        dilateDisk(alpha, w, h, -shiftPx);
        for (auto& v : alpha) v = 1.f - v;
    }
}

// ------------------------------------------------------------------------------------------- resampling
// Block-average downsample by an integer factor (output size ceil(w/s) x ceil(h/s)).
static void boxDown(const Plane& in, int w, int h, int s, Plane& out, int& ws, int& hs) {
    ws = (w + s - 1) / s; hs = (h + s - 1) / s;
    out.assign(size_t(ws) * hs, 0.f);
    parallelFor(hs, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y)
            for (int x = 0; x < ws; ++x) {
                float sum = 0.f; int n = 0;
                for (int j = y * s; j < std::min(h, y * s + s); ++j)
                    for (int i = x * s; i < std::min(w, x * s + s); ++i) { sum += in[size_t(j) * w + i]; ++n; }
                out[size_t(y) * ws + x] = sum / float(n);
            }
    });
}

// Bilinear upsample of a (ws x hs) plane whose samples are block centres of factor s, to (w x h).
static void upBilinear(const Plane& in, int ws, int hs, int s, int w, int h, Plane& out) {
    out.resize(size_t(w) * h);
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            float fy = (y + 0.5f) / s - 0.5f;
            int iy = int(std::floor(fy)); float ty = fy - iy;
            int ya = std::min(std::max(iy, 0), hs - 1), yb = std::min(std::max(iy + 1, 0), hs - 1);
            const float* ra = &in[size_t(ya) * ws]; const float* rb = &in[size_t(yb) * ws];
            float* o = &out[size_t(y) * w];
            for (int x = 0; x < w; ++x) {
                float fx = (x + 0.5f) / s - 0.5f;
                int ix = int(std::floor(fx)); float tx = fx - ix;
                int xa = std::min(std::max(ix, 0), ws - 1), xb = std::min(std::max(ix + 1, 0), ws - 1);
                o[x] = (ra[xa] * (1 - tx) + ra[xb] * tx) * (1 - ty) + (rb[xa] * (1 - tx) + rb[xb] * tx) * ty;
            }
        }
    });
}

// ------------------------------------------------------------------------------------------- guided filter
// Mean linear coefficients (a, b) of the guided filter q = a*I + b for guide I and input p.
static void guidedCoeffs(const Plane& I, const Plane& p, int w, int h, int r, float eps, Plane& ma, Plane& mb) {
    size_t n = p.size();
    Plane mI(n), mp(n), cIp(n), cII(n), tmp(n), buf(n);
    boxBlur(I.data(), mI.data(), tmp.data(), w, h, r);
    boxBlur(p.data(), mp.data(), tmp.data(), w, h, r);
    for (size_t i = 0; i < n; ++i) buf[i] = I[i] * p[i];
    boxBlur(buf.data(), cIp.data(), tmp.data(), w, h, r);
    for (size_t i = 0; i < n; ++i) buf[i] = I[i] * I[i];
    boxBlur(buf.data(), cII.data(), tmp.data(), w, h, r);
    for (size_t i = 0; i < n; ++i) {
        float var = cII[i] - mI[i] * mI[i];
        float cov = cIp[i] - mI[i] * mp[i];
        float ai = cov / (var + eps);
        cII[i] = mp[i] - ai * mI[i];     // b
        cIp[i] = ai;                     // a
    }
    ma.resize(n); mb.resize(n);
    boxBlur(cIp.data(), ma.data(), tmp.data(), w, h, r);
    boxBlur(cII.data(), mb.data(), tmp.data(), w, h, r);
}

// Fast guided filter (He & Sun 2015): coefficients are solved on a `sub`-times smaller grid and upsampled, the
// full-resolution guide is applied at the end, so edges stay exact while the cost drops ~sub^2.
static void guidedFilter(const Plane& I, Plane& p, int w, int h, int r, float eps, int sub) {
    if (sub <= 1 || std::min(w, h) < 128) {
        Plane ma, mb;
        guidedCoeffs(I, p, w, h, r, eps, ma, mb);
        for (size_t i = 0; i < p.size(); ++i) p[i] = ma[i] * I[i] + mb[i];
        return;
    }
    Plane Is, ps, ma, mb, A, B;
    int ws, hs;
    boxDown(I, w, h, sub, Is, ws, hs);
    boxDown(p, w, h, sub, ps, ws, hs);
    guidedCoeffs(Is, ps, ws, hs, std::max(1, r / sub), eps, ma, mb);
    upBilinear(ma, ws, hs, sub, w, h, A);
    upBilinear(mb, ws, hs, sub, w, h, B);
    for (size_t i = 0; i < p.size(); ++i) p[i] = A[i] * I[i] + B[i];
}

// ------------------------------------------------------------------------------------------- pipeline
// One iteration of Germer et al. "approximate fast foreground colour estimation" (blur fusion) on a w x h grid.
static void fusionStage(const Plane* I, const Plane& aa, const Plane* Fin, const Plane* Bin, int w, int h, int r,
                        Plane* Fout, Plane* Bout) {
    const size_t n = size_t(w) * h;
    Plane ba(n), fa(n), b1a(n), t1(n), t2(n), tmp(n);
    boxBlur(aa.data(), ba.data(), tmp.data(), w, h, r);
    for (int c = 0; c < 3; ++c) {
        for (size_t i = 0; i < n; ++i) t1[i] = Fin[c][i] * aa[i];
        boxBlur(t1.data(), fa.data(), tmp.data(), w, h, r);
        for (size_t i = 0; i < n; ++i) t2[i] = Bin[c][i] * (1.f - aa[i]);
        boxBlur(t2.data(), b1a.data(), tmp.data(), w, h, r);
        Plane& bF = Fout[c]; Plane& bB = Bout[c];
        bF.resize(n); bB.resize(n);
        for (size_t i = 0; i < n; ++i) {
            float blurF = fa[i] / (ba[i] + 1e-5f);
            float blurB = b1a[i] / ((1.f - ba[i]) + 1e-5f);
            bF[i] = blurF + aa[i] * (I[c][i] - aa[i] * blurF - (1.f - aa[i]) * blurB);
            bB[i] = blurB;
        }
    }
}

struct Roi { int x0, y0, x1, y1; int w() const { return x1 - x0; } int h() const { return y1 - y0; } };

// Bilinear sample of the top-down matte at memory-order pixel centre (x,y) of a WxH frame.
static inline float sampleMatte(const Matte& m, float u, float v) {
    float fx = u * m.w - 0.5f, fy = v * m.h - 0.5f;
    int x0 = int(std::floor(fx)), y0 = int(std::floor(fy));
    float tx = fx - x0, ty = fy - y0;
    auto at = [&](int x, int y) {
        x = std::min(std::max(x, 0), m.w - 1); y = std::min(std::max(y, 0), m.h - 1);
        return m.a[size_t(y) * m.w + x] * (1.f / 255.f);
    };
    float a = at(x0, y0), b = at(x0 + 1, y0), c = at(x0, y0 + 1), d = at(x0 + 1, y0 + 1);
    return (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty;
}

static bool matteBounds(const Matte& m, int& x0, int& y0, int& x1, int& y1) {
    x0 = m.w; y0 = m.h; x1 = -1; y1 = -1;
    for (int y = 0; y < m.h; ++y) {
        const uint8_t* r = &m.a[size_t(y) * m.w];
        for (int x = 0; x < m.w; ++x) if (r[x] > 2) {
            x0 = std::min(x0, x); x1 = std::max(x1, x); y0 = std::min(y0, y); y1 = std::max(y1, y);
        }
    }
    return x1 >= 0;
}

struct Work {
    Roi roi;
    int w = 0, h = 0;
    Plane raw;        // upsampled matte, no edge processing (ROI)
    Plane alpha;      // final alpha (ROI)
    Plane F[3];       // decontaminated colour (ROI) when requested
    bool haveF = false;
};

static void buildRoi(const RGBAImage& src, const Matte& m, const EdgeParams& ep, bool flipY, float scale, Roi& roi, bool& any) {
    int bx0, by0, bx1, by1;
    any = matteBounds(m, bx0, by0, bx1, by1);
    if (!any) { roi = {0, 0, 0, 0}; return; }
    float sx = float(src.w) / m.w, sy = float(src.h) / m.h;
    int longEdge = std::max(src.w, src.h);
    float margin = std::fabs(ep.edgeShift) * scale + ep.feather * scale * 3.f + ep.smooth * scale * 3.f +
                   0.012f * longEdge * (1 + ep.quality) + (ep.decontaminate > 0 || ep.spill > 0 ? 0.05f * longEdge : 0.f) + 8.f;
    int mx0 = int(std::floor(bx0 * sx - margin)), mx1 = int(std::ceil((bx1 + 1) * sx + margin));
    int my0 = int(std::floor(by0 * sy - margin)), my1 = int(std::ceil((by1 + 1) * sy + margin));
    if (flipY) { int t0 = src.h - my1, t1 = src.h - my0; my0 = t0; my1 = t1; }
    roi.x0 = std::max(0, mx0); roi.x1 = std::min(src.w, mx1);
    roi.y0 = std::max(0, my0); roi.y1 = std::min(src.h, my1);
    if (roi.w() <= 0 || roi.h() <= 0) { any = false; }
}

// Full edge pipeline on the ROI. Fills wk.raw, wk.alpha (and wk.F when decontaminating).
static void runPipeline(const RGBAImage& src, const Matte& m, const EdgeParams& ep, bool flipY, float scale, Work& wk, bool wantColour) {
    const Roi roi = wk.roi;
    const int w = roi.w(), h = roi.h();
    wk.w = w; wk.h = h;
    const size_t n = size_t(w) * h;
    wk.raw.assign(n, 0.f);

    // 1. upsample the analysis-resolution matte
    parallelFor(h, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            int my = roi.y0 + y;
            float v = (my + 0.5f) / src.h;
            if (flipY) v = 1.f - v;
            float* o = &wk.raw[size_t(y) * w];
            for (int x = 0; x < w; ++x) o[x] = sampleMatte(m, (roi.x0 + x + 0.5f) / src.w, v);
        }
    });

    Plane a = wk.raw, tmp(n);

    // 2. noise removal: levels cleanup
    {
        float lo = ep.cleanBlack, hi = std::max(ep.cleanWhite, lo + 1e-3f), inv = 1.f / (hi - lo);
        for (auto& v : a) v = clamp01((v - lo) * inv);
    }
    // 3. morphology
    shiftEdge(a, w, h, ep.edgeShift * scale);

    // 4. guided edge refinement (uses the image as guide, restricted to the uncertain band)
    Plane guide(n);
    const bool needGuide = ep.refine > 0.01f || ep.decontaminate > 0.f;
    if (needGuide) {
        parallelFor(h, [&](int y0, int y1) {
            for (int y = y0; y < y1; ++y) {
                const float* s = src.px(roi.x0, roi.y0 + y);
                float* g = &guide[size_t(y) * w];
                for (int x = 0; x < w; ++x, s += 4)
                    g[x] = clamp01(0.299f * s[0] + 0.587f * s[1] + 0.114f * s[2]);
            }
        });
    }
    if (ep.refine > 0.01f) {
        int longEdge = std::max(src.w, src.h);
        int r = std::max(2, int(std::lround((0.0015f + 0.0015f * ep.quality) * longEdge * (0.5f + ep.refine))));
        Plane q = a;
        guidedFilter(guide, q, w, h, r, 2e-4f, ep.quality >= 2 ? 1 : (ep.quality == 1 ? 2 : 4));
        // uncertain band = where alpha is neither 0 nor 1, widened by the filter radius
        Plane band(n);
        for (size_t i = 0; i < n; ++i) band[i] = (a[i] > 0.004f && a[i] < 0.996f) ? 1.f : 0.f;
        Plane bb(n);
        boxBlur(band.data(), bb.data(), tmp.data(), w, h, r);
        // tightening: the bilinear upsample of a low-res matte is blurry; sharpen proportionally to `refine`
        float gain = 1.f + 3.f * ep.refine;
        for (size_t i = 0; i < n; ++i) {
            float wgt = std::min(1.f, bb[i] * 3.f) * ep.refine;
            float qq = clamp01((clamp01(q[i]) - 0.5f) * gain + 0.5f);
            a[i] = a[i] * (1.f - wgt) + qq * wgt;
        }
    }
    // 5. shape smoothing: blur then re-steepen so rounding does not soften the edge
    if (ep.smooth * scale > 0.3f) {
        Plane s = a;
        gaussBlur(s, tmp, w, h, ep.smooth * scale);
        for (size_t i = 0; i < n; ++i) a[i] = clamp01((s[i] - 0.5f) * 2.5f + 0.5f) * 0.85f + a[i] * 0.15f;
    }
    // 6. feather
    if (ep.feather * scale > 0.3f) gaussBlur(a, tmp, w, h, ep.feather * scale);

    // 7. decontamination / spill (foreground colour estimation, Germer et al. blur fusion)
    wk.haveF = false;
    if (wantColour && (ep.decontaminate > 0.f || ep.spill > 0.f)) {
        for (int c = 0; c < 3; ++c) wk.F[c].assign(n, 0.f);
        Plane I[3], Fh[3], Bh[3], aa = a;
        for (int c = 0; c < 3; ++c) I[c].resize(n);
        parallelFor(h, [&](int y0, int y1) {
            for (int y = y0; y < y1; ++y) {
                const float* s = src.px(roi.x0, roi.y0 + y);
                for (int x = 0; x < w; ++x, s += 4)
                    for (int c = 0; c < 3; ++c) I[c][size_t(y) * w + x] = s[c];
            }
        });
        int longEdge = std::max(w, h);
        int r1 = std::max(6, int(0.04f * longEdge)), r2 = std::max(2, int(0.004f * longEdge));
        Plane F1[3], B1[3];
        // Stage 1 uses a very large radius, i.e. a smooth estimate: compute it on a 4x smaller grid and upsample.
        const int sub = std::min(w, h) >= 256 ? 4 : 1;
        if (sub > 1) {
            Plane Is[3], as, F1s[3], B1s[3];
            int ws = 0, hs = 0;
            for (int c = 0; c < 3; ++c) boxDown(I[c], w, h, sub, Is[c], ws, hs);
            boxDown(aa, w, h, sub, as, ws, hs);
            fusionStage(Is, as, Is, Is, ws, hs, std::max(2, r1 / sub), F1s, B1s);
            for (int c = 0; c < 3; ++c) { upBilinear(F1s[c], ws, hs, sub, w, h, F1[c]); upBilinear(B1s[c], ws, hs, sub, w, h, B1[c]); }
        } else {
            fusionStage(I, aa, I, I, w, h, r1, F1, B1);
        }
        for (int c = 0; c < 3; ++c) for (auto& v : F1[c]) v = std::max(v, 0.f);
        fusionStage(I, aa, F1, B1, w, h, r2, Fh, Bh);

        // spill tint from the estimated background colour along the edge
        int key = -1;
        if (ep.spill > 0.f) {
            double sum[3] = {0, 0, 0}, wsum = 0;
            for (size_t i = 0; i < n; ++i) if (a[i] > 0.02f && a[i] < 0.98f) {
                float wgt = a[i] * (1.f - a[i]);
                for (int c = 0; c < 3; ++c) sum[c] += B1[c][i] * wgt;
                wsum += wgt;
            }
            if (wsum > 0) {
                float bm[3] = {float(sum[0] / wsum), float(sum[1] / wsum), float(sum[2] / wsum)};
                int k = int(std::max_element(bm, bm + 3) - bm);
                float others = (bm[(k + 1) % 3] + bm[(k + 2) % 3]) * 0.5f;
                if (bm[k] - others > 0.06f) key = k;
            }
        }
        for (size_t i = 0; i < n; ++i) {
            float f[3];
            for (int c = 0; c < 3; ++c) {
                float fc = std::max(Fh[c][i], 0.f);
                f[c] = I[c][i] + ep.decontaminate * (fc - I[c][i]);
            }
            if (key >= 0 && a[i] > 0.f) {
                float lim = (f[(key + 1) % 3] + f[(key + 2) % 3]) * 0.5f;
                if (f[key] > lim) f[key] -= ep.spill * (f[key] - lim);
            }
            for (int c = 0; c < 3; ++c) wk.F[c][i] = f[c];
        }
        wk.haveF = true;
    }
    wk.alpha.swap(a);
}

void computeAlpha(const RGBAImage& src, const Matte& m, const EdgeParams& ep, bool flipY, float* outAlpha) {
    std::fill(outAlpha, outAlpha + size_t(src.w) * src.h, 0.f);
    Work wk;
    bool any;
    buildRoi(src, m, ep, flipY, 1.f, wk.roi, any);
    if (!any) return;
    runPipeline(src, m, ep, flipY, 1.f, wk, false);
    for (int y = 0; y < wk.h; ++y)
        std::memcpy(outAlpha + size_t(wk.roi.y0 + y) * src.w + wk.roi.x0, &wk.alpha[size_t(y) * wk.w], sizeof(float) * wk.w);
}

// ------------------------------------------------------------------------------------------- output
static inline float checker(int x, int y, int size) {
    return (((x / size) + (y / size)) & 1) ? 0.62f : 0.38f;
}

void render(const RGBAImage& src, const Matte* matte, const RenderParams& rp, RGBAImage& dst) {
    const int W = src.w, H = src.h;
    auto copySrc = [&](float alphaOverride) {
        parallelFor(H, [&](int y0, int y1) {
            for (int y = y0; y < y1; ++y) {
                const float* s = src.px(0, y); float* d = dst.px(0, y);
                for (int x = 0; x < W; ++x, s += 4, d += 4) {
                    d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = alphaOverride >= 0 ? alphaOverride : s[3];
                }
            }
        });
    };
    if (rp.mode == kOriginal || matte == nullptr || !matte->valid()) { copySrc(rp.mode == kOriginal ? -1.f : 1.f); return; }

    const float scale = rp.renderScale > 0 ? rp.renderScale : 1.f;
    Work wk;
    bool any;
    buildRoi(src, *matte, rp.edge, rp.matteFlipY, scale, wk.roi, any);

    const bool wantColour = rp.mode == kCutout || rp.mode == kComposite || rp.mode == kCheckerboard;
    if (any) runPipeline(src, *matte, rp.edge, rp.matteFlipY, scale, wk, wantColour);

    auto alphaAt = [&](int x, int y, const Plane& pl) -> float {
        if (!any) return 0.f;
        int rx = x - wk.roi.x0, ry = y - wk.roi.y0;
        if (rx < 0 || ry < 0 || rx >= wk.w || ry >= wk.h) return 0.f;
        return pl[size_t(ry) * wk.w + rx];
    };

    parallelFor(H, [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const float* s = src.px(0, y); float* d = dst.px(0, y);
            // checkerboard cell index uses the top-down row so it does not flip with OFX orientation
            int ty = rp.matteFlipY ? (H - 1 - y) : y;
            for (int x = 0; x < W; ++x, s += 4, d += 4) {
                float a = alphaAt(x, y, wk.alpha);
                float rgb[3] = {s[0], s[1], s[2]};
                if (wk.haveF && any) {
                    int rx = x - wk.roi.x0, ry = y - wk.roi.y0;
                    if (rx >= 0 && ry >= 0 && rx < wk.w && ry < wk.h) {
                        size_t i = size_t(ry) * wk.w + rx;
                        rgb[0] = wk.F[0][i]; rgb[1] = wk.F[1][i]; rgb[2] = wk.F[2][i];
                    }
                }
                switch (rp.mode) {
                    case kMask: {
                        float m = alphaAt(x, y, wk.raw);
                        d[0] = d[1] = d[2] = m; d[3] = 1.f; break; }
                    case kAlpha:
                        d[0] = d[1] = d[2] = a; d[3] = 1.f; break;
                    case kCutout:
                        if (rp.premultiply) { d[0] = rgb[0] * a; d[1] = rgb[1] * a; d[2] = rgb[2] * a; }
                        else { d[0] = rgb[0]; d[1] = rgb[1]; d[2] = rgb[2]; }
                        d[3] = a; break;
                    case kOverlay: {
                        float k = rp.overlayOpacity * a;
                        for (int c = 0; c < 3; ++c) d[c] = s[c] * (1.f - k) + rp.overlayColor[c] * k;
                        d[3] = 1.f; break; }
                    case kComposite:
                    case kCheckerboard: {
                        float bg[3];
                        if (rp.mode == kCheckerboard || rp.bgKind == kBgChecker) {
                            float c = checker(x, ty, std::max(4, int(rp.checkerSize * scale)));
                            bg[0] = bg[1] = bg[2] = c;
                        } else if (rp.bgKind == kBgClip && rp.bgImage && rp.bgImage->data) {
                            int bx = std::min(x, rp.bgImage->w - 1), by = std::min(y, rp.bgImage->h - 1);
                            const float* b = rp.bgImage->px(bx, by);
                            bg[0] = b[0]; bg[1] = b[1]; bg[2] = b[2];
                        } else {
                            bg[0] = rp.bgColor[0]; bg[1] = rp.bgColor[1]; bg[2] = rp.bgColor[2];
                        }
                        for (int c = 0; c < 3; ++c) d[c] = rgb[c] * a + bg[c] * (1.f - a);
                        d[3] = 1.f; break; }
                    default:
                        d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = s[3];
                }
            }
        }
    });
}

}  // namespace acut
