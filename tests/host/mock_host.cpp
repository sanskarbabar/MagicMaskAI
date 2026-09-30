// Minimal OpenFX host for regression-testing plugins outside Resolve.
//
// Loads a .ofx DLL and drives it through the real action protocol (Load, Describe, DescribeInContext,
// CreateInstance, Render, IsIdentity, InstanceChanged, overlay interact actions) using real OFX property /
// parameter / image-effect / message / draw suites. It is a test tool, not a general host: only what AI Cutout needs.
//
//   mock_host <plugin.ofx> <workdir> <command>...
// commands:
//   set:<param>=<value>        set a parameter (type-aware)
//   get:<param>                print a parameter's value
//   render:<name>[@<time>]     render; writes <workdir>/<name>.f32 (top-down float RGBA, int w,h header)
//   identity[@<time>]          call IsIdentity and print the result
//   changed:<param>            deliver kOfxActionInstanceChanged (user edit)
//   draw                       overlay draw action; prints the text drawn
//   pen:<x>,<y>                overlay pen down + up at canonical coords
//   wait:<ms>                  sleep
//   dump                       print all parameter names
// Source image: <workdir>/src.f32 (int w, int h, float rgba[h][w][4], top-down). The host stores it bottom-up
// (OFX convention). Env MOCK_NEGATIVE_ROWBYTES=1 hands the image out with a negative row stride.
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "ofxCore.h"
#include "ofxDrawSuite.h"
#include "ofxImageEffect.h"
#include "ofxInteract.h"
#include "ofxMemory.h"
#include "ofxMessage.h"
#include "ofxMultiThread.h"
#include "ofxParam.h"
#include "ofxProperty.h"

// ------------------------------------------------------------------------------------------------ properties
struct Prop {
    std::vector<std::string> s;
    std::vector<int> i;
    std::vector<double> d;
    std::vector<void*> p;
    char kind = 0;   // 's','i','d','p'
};
struct PropSet { std::map<std::string, Prop> m; };

static OfxStatus getProp(OfxPropertySetHandle h, const char* n, int idx, char kind, Prop** out) {
    auto* ps = reinterpret_cast<PropSet*>(h);
    if (!ps) return kOfxStatErrBadHandle;
    auto it = ps->m.find(n);
    if (it == ps->m.end() || it->second.kind != kind) {
        if (std::getenv("MOCK_TRACE")) std::fprintf(stderr, "HOST: property '%s'[%d] kind %c not available\n", n, idx, kind);
        return kOfxStatErrUnknown;
    }
    size_t sz = kind == 's' ? it->second.s.size() : kind == 'i' ? it->second.i.size() : kind == 'd' ? it->second.d.size() : it->second.p.size();
    if (idx < 0 || size_t(idx) >= sz) return kOfxStatErrBadIndex;
    *out = &it->second;
    return kOfxStatOK;
}
template <class T> static void setAt(std::vector<T>& v, int idx, T val) { if (size_t(idx) >= v.size()) v.resize(size_t(idx) + 1); v[size_t(idx)] = val; }

static OfxStatus pSetS(OfxPropertySetHandle h, const char* n, int i, const char* v) { auto& p = reinterpret_cast<PropSet*>(h)->m[n]; p.kind = 's'; setAt(p.s, i, std::string(v)); return kOfxStatOK; }
static OfxStatus pSetD(OfxPropertySetHandle h, const char* n, int i, double v) { auto& p = reinterpret_cast<PropSet*>(h)->m[n]; p.kind = 'd'; setAt(p.d, i, v); return kOfxStatOK; }
static OfxStatus pSetI(OfxPropertySetHandle h, const char* n, int i, int v) { auto& p = reinterpret_cast<PropSet*>(h)->m[n]; p.kind = 'i'; setAt(p.i, i, v); return kOfxStatOK; }
static OfxStatus pSetP(OfxPropertySetHandle h, const char* n, int i, void* v) { auto& p = reinterpret_cast<PropSet*>(h)->m[n]; p.kind = 'p'; setAt(p.p, i, v); return kOfxStatOK; }
static OfxStatus pSetPN(OfxPropertySetHandle h, const char* n, int c, void* const* v) { for (int k = 0; k < c; ++k) pSetP(h, n, k, v[k]); return kOfxStatOK; }
static OfxStatus pSetSN(OfxPropertySetHandle h, const char* n, int c, const char* const* v) { for (int k = 0; k < c; ++k) pSetS(h, n, k, v[k]); return kOfxStatOK; }
static OfxStatus pSetDN(OfxPropertySetHandle h, const char* n, int c, const double* v) { for (int k = 0; k < c; ++k) pSetD(h, n, k, v[k]); return kOfxStatOK; }
static OfxStatus pSetIN(OfxPropertySetHandle h, const char* n, int c, const int* v) { for (int k = 0; k < c; ++k) pSetI(h, n, k, v[k]); return kOfxStatOK; }
static OfxStatus pGetS(OfxPropertySetHandle h, const char* n, int i, char** v) { Prop* p; auto s = getProp(h, n, i, 's', &p); if (s) return s; *v = const_cast<char*>(p->s[size_t(i)].c_str()); return kOfxStatOK; }
static OfxStatus pGetD(OfxPropertySetHandle h, const char* n, int i, double* v) { Prop* p; auto s = getProp(h, n, i, 'd', &p); if (s) return s; *v = p->d[size_t(i)]; return kOfxStatOK; }
static OfxStatus pGetI(OfxPropertySetHandle h, const char* n, int i, int* v) { Prop* p; auto s = getProp(h, n, i, 'i', &p); if (s) return s; *v = p->i[size_t(i)]; return kOfxStatOK; }
static OfxStatus pGetP(OfxPropertySetHandle h, const char* n, int i, void** v) { Prop* p; auto s = getProp(h, n, i, 'p', &p); if (s) return s; *v = p->p[size_t(i)]; return kOfxStatOK; }
static OfxStatus pGetPN(OfxPropertySetHandle h, const char* n, int c, void** v) { for (int k = 0; k < c; ++k) { auto s = pGetP(h, n, k, &v[k]); if (s) return s; } return kOfxStatOK; }
static OfxStatus pGetSN(OfxPropertySetHandle h, const char* n, int c, char** v) { for (int k = 0; k < c; ++k) { auto s = pGetS(h, n, k, &v[k]); if (s) return s; } return kOfxStatOK; }
static OfxStatus pGetDN(OfxPropertySetHandle h, const char* n, int c, double* v) { for (int k = 0; k < c; ++k) { auto s = pGetD(h, n, k, &v[k]); if (s) return s; } return kOfxStatOK; }
static OfxStatus pGetIN(OfxPropertySetHandle h, const char* n, int c, int* v) { for (int k = 0; k < c; ++k) { auto s = pGetI(h, n, k, &v[k]); if (s) return s; } return kOfxStatOK; }
static OfxStatus pReset(OfxPropertySetHandle h, const char* n) { reinterpret_cast<PropSet*>(h)->m.erase(n); return kOfxStatOK; }
static OfxStatus pDim(OfxPropertySetHandle h, const char* n, int* c) {
    auto* ps = reinterpret_cast<PropSet*>(h); auto it = ps->m.find(n);
    if (it == ps->m.end()) { *c = 0; return kOfxStatOK; }      // real hosts pre-create descriptor properties (dimension 0)
    auto& p = it->second;
    *c = int(p.kind == 's' ? p.s.size() : p.kind == 'i' ? p.i.size() : p.kind == 'd' ? p.d.size() : p.p.size());
    return kOfxStatOK;
}
static OfxPropertySuiteV1 gPropSuite = {pSetP, pSetS, pSetD, pSetI, pSetPN, pSetSN, pSetDN, pSetIN,
                                        pGetP, pGetS, pGetD, pGetI, pGetPN, pGetSN, pGetDN, pGetIN, pReset, pDim};

// ------------------------------------------------------------------------------------------------ params / clips / effects
struct Param {
    std::string name, type;
    PropSet props;
    std::vector<int> iv;
    std::vector<double> dv;
    std::string sv;
};
struct ParamSet {
    PropSet props;
    std::map<std::string, std::unique_ptr<Param>> params;
    std::vector<std::string> order;
};
struct Clip { std::string name; PropSet props; };
struct Effect {
    PropSet props;
    ParamSet ps;
    std::map<std::string, std::unique_ptr<Clip>> clips;
    std::string context;
};

static std::vector<std::string> gMessages;
static std::vector<std::string> gDrawText;
static int gDrawCalls = 0, gRedraws = 0;
static Effect* gInstance = nullptr;

static OfxStatus paramDefine(OfxParamSetHandle h, const char* type, const char* name, OfxPropertySetHandle* out) {
    auto* ps = reinterpret_cast<ParamSet*>(h);
    auto p = std::make_unique<Param>();
    p->name = name; p->type = type;
    pSetS(reinterpret_cast<OfxPropertySetHandle>(&p->props), kOfxParamPropType, 0, type);
    pSetS(reinterpret_cast<OfxPropertySetHandle>(&p->props), kOfxPropName, 0, name);
    *out = reinterpret_cast<OfxPropertySetHandle>(&p->props);
    ps->order.push_back(name);
    ps->params[name] = std::move(p);
    return kOfxStatOK;
}
static OfxStatus paramGetHandle(OfxParamSetHandle h, const char* name, OfxParamHandle* p, OfxPropertySetHandle* props) {
    auto* ps = reinterpret_cast<ParamSet*>(h);
    auto it = ps->params.find(name);
    if (it == ps->params.end()) { std::fprintf(stderr, "HOST: plugin asked for undefined param '%s'\n", name); return kOfxStatErrUnknown; }
    *p = reinterpret_cast<OfxParamHandle>(it->second.get());
    if (props) *props = reinterpret_cast<OfxPropertySetHandle>(&it->second->props);
    return kOfxStatOK;
}
static OfxStatus paramSetGetProps(OfxParamSetHandle h, OfxPropertySetHandle* out) { *out = reinterpret_cast<OfxPropertySetHandle>(&reinterpret_cast<ParamSet*>(h)->props); return kOfxStatOK; }
static OfxStatus paramGetProps(OfxParamHandle h, OfxPropertySetHandle* out) { *out = reinterpret_cast<OfxPropertySetHandle>(&reinterpret_cast<Param*>(h)->props); return kOfxStatOK; }

static bool isType(const Param* p, const char* t) { return p->type == t; }
static OfxStatus paramGetV(OfxParamHandle h, va_list ap) {
    auto* p = reinterpret_cast<Param*>(h);
    if (isType(p, kOfxParamTypeInteger) || isType(p, kOfxParamTypeBoolean) || isType(p, kOfxParamTypeChoice)) { *va_arg(ap, int*) = p->iv.empty() ? 0 : p->iv[0]; }
    else if (isType(p, kOfxParamTypeDouble)) { *va_arg(ap, double*) = p->dv.empty() ? 0 : p->dv[0]; }
    else if (isType(p, kOfxParamTypeRGB)) { for (int k = 0; k < 3; ++k) *va_arg(ap, double*) = p->dv.size() > size_t(k) ? p->dv[size_t(k)] : 0; }
    else if (isType(p, kOfxParamTypeRGBA)) { for (int k = 0; k < 4; ++k) *va_arg(ap, double*) = p->dv.size() > size_t(k) ? p->dv[size_t(k)] : 0; }
    else if (isType(p, kOfxParamTypeString) || isType(p, kOfxParamTypeCustom)) { *va_arg(ap, const char**) = p->sv.c_str(); }
    else if (isType(p, kOfxParamTypeInteger2D)) { for (int k = 0; k < 2; ++k) *va_arg(ap, int*) = p->iv.size() > size_t(k) ? p->iv[size_t(k)] : 0; }
    else if (isType(p, kOfxParamTypeDouble2D)) { for (int k = 0; k < 2; ++k) *va_arg(ap, double*) = p->dv.size() > size_t(k) ? p->dv[size_t(k)] : 0; }
    else return kOfxStatErrUnsupported;
    return kOfxStatOK;
}
static OfxStatus paramGetValue(OfxParamHandle h, ...) { va_list ap; va_start(ap, h); auto s = paramGetV(h, ap); va_end(ap); return s; }
static OfxStatus paramGetValueAtTime(OfxParamHandle h, OfxTime, ...) { va_list ap; va_start(ap, h); auto s = paramGetV(h, ap); va_end(ap); return s; }
static OfxStatus paramSetValue(OfxParamHandle h, ...) {
    auto* p = reinterpret_cast<Param*>(h);
    va_list ap; va_start(ap, h);
    if (isType(p, kOfxParamTypeInteger) || isType(p, kOfxParamTypeBoolean) || isType(p, kOfxParamTypeChoice)) p->iv = {va_arg(ap, int)};
    else if (isType(p, kOfxParamTypeDouble)) p->dv = {va_arg(ap, double)};
    else if (isType(p, kOfxParamTypeRGB)) { p->dv.clear(); for (int k = 0; k < 3; ++k) p->dv.push_back(va_arg(ap, double)); }
    else if (isType(p, kOfxParamTypeRGBA)) { p->dv.clear(); for (int k = 0; k < 4; ++k) p->dv.push_back(va_arg(ap, double)); }
    else if (isType(p, kOfxParamTypeString) || isType(p, kOfxParamTypeCustom)) p->sv = va_arg(ap, const char*);
    va_end(ap);
    return kOfxStatOK;
}
static OfxStatus paramNoop1(OfxParamSetHandle, const char*) { return kOfxStatOK; }
static OfxStatus paramNoop0(OfxParamSetHandle) { return kOfxStatOK; }
static OfxStatus paramUnsupported(OfxParamHandle) { return kOfxStatErrUnsupported; }

static OfxParameterSuiteV1 gParamSuite;

static void initParamSuite() {
    std::memset(&gParamSuite, 0, sizeof(gParamSuite));
    gParamSuite.paramDefine = paramDefine;
    gParamSuite.paramGetHandle = paramGetHandle;
    gParamSuite.paramSetGetPropertySet = paramSetGetProps;
    gParamSuite.paramGetPropertySet = paramGetProps;
    gParamSuite.paramGetValue = paramGetValue;
    gParamSuite.paramGetValueAtTime = paramGetValueAtTime;
    gParamSuite.paramSetValue = paramSetValue;
    gParamSuite.paramEditBegin = paramNoop1;
    gParamSuite.paramEditEnd = paramNoop0;
    gParamSuite.paramDeleteAllKeys = paramUnsupported;
}

// ---- image effect suite
static OfxStatus ieGetPropertySet(OfxImageEffectHandle h, OfxPropertySetHandle* out) { *out = reinterpret_cast<OfxPropertySetHandle>(&reinterpret_cast<Effect*>(h)->props); return kOfxStatOK; }
static OfxStatus ieGetParamSet(OfxImageEffectHandle h, OfxParamSetHandle* out) { *out = reinterpret_cast<OfxParamSetHandle>(&reinterpret_cast<Effect*>(h)->ps); return kOfxStatOK; }
static OfxStatus ieClipDefine(OfxImageEffectHandle h, const char* name, OfxPropertySetHandle* out) {
    auto* e = reinterpret_cast<Effect*>(h);
    auto c = std::make_unique<Clip>(); c->name = name;
    *out = reinterpret_cast<OfxPropertySetHandle>(&c->props);
    e->clips[name] = std::move(c);
    return kOfxStatOK;
}
static OfxStatus ieClipGetHandle(OfxImageEffectHandle h, const char* name, OfxImageClipHandle* c, OfxPropertySetHandle* props) {
    auto* e = reinterpret_cast<Effect*>(h);
    auto it = e->clips.find(name);
    if (it == e->clips.end()) { std::fprintf(stderr, "HOST: plugin asked for undefined clip '%s'\n", name); return kOfxStatErrUnknown; }
    *c = reinterpret_cast<OfxImageClipHandle>(it->second.get());
    if (props) *props = reinterpret_cast<OfxPropertySetHandle>(&it->second->props);
    return kOfxStatOK;
}
static OfxStatus ieClipGetPropertySet(OfxImageClipHandle c, OfxPropertySetHandle* out) { *out = reinterpret_cast<OfxPropertySetHandle>(&reinterpret_cast<Clip*>(c)->props); return kOfxStatOK; }

static int gW = 0, gH = 0;
static std::vector<float> gSrcTopDown;          // rgba top-down
static std::vector<float> gStoreSrc, gStoreDst;  // host-owned buffers
static bool gNegRowBytes = false;
static std::vector<std::unique_ptr<PropSet>> gImages;

static PropSet* makeImage(std::vector<float>& store, bool flipUpload) {
    auto img = std::make_unique<PropSet>();
    auto* h = reinterpret_cast<OfxPropertySetHandle>(img.get());
    const int rowBytes = gW * 4 * int(sizeof(float));
    char* base = reinterpret_cast<char*>(store.data());
    void* data = base;
    int rb = rowBytes;
    if (gNegRowBytes) { data = base + size_t(gH - 1) * rowBytes; rb = -rowBytes; }   // memory top-down, data = bottom scanline
    (void)flipUpload;
    pSetP(h, kOfxImagePropData, 0, data);
    int bounds[4] = {0, 0, gW, gH};
    pSetIN(h, kOfxImagePropBounds, 4, bounds);
    pSetIN(h, kOfxImagePropRegionOfDefinition, 4, bounds);
    pSetI(h, kOfxImagePropRowBytes, 0, rb);
    pSetD(h, kOfxImagePropPixelAspectRatio, 0, 1.0);
    pSetS(h, kOfxImageEffectPropPixelDepth, 0, kOfxBitDepthFloat);
    pSetS(h, kOfxImageEffectPropComponents, 0, kOfxImageComponentRGBA);
    pSetS(h, kOfxImageEffectPropPreMultiplication, 0, kOfxImageUnPreMultiplied);
    double rs[2] = {1, 1};
    pSetDN(h, kOfxImageEffectPropRenderScale, 2, rs);
    pSetS(h, kOfxImagePropField, 0, kOfxImageFieldNone);
    pSetS(h, kOfxImagePropUniqueIdentifier, 0, "img");
    gImages.push_back(std::move(img));
    return gImages.back().get();
}
static OfxStatus ieClipGetImage(OfxImageClipHandle c, OfxTime, const OfxRectD*, OfxPropertySetHandle* out) {
    auto* clip = reinterpret_cast<Clip*>(c);
    if (clip->name == "Background") return kOfxStatFailed;
    *out = reinterpret_cast<OfxPropertySetHandle>(makeImage(clip->name == kOfxImageEffectOutputClipName ? gStoreDst : gStoreSrc, false));
    return kOfxStatOK;
}
static OfxStatus ieClipReleaseImage(OfxPropertySetHandle h) {
    for (auto it = gImages.begin(); it != gImages.end(); ++it) if (reinterpret_cast<OfxPropertySetHandle>(it->get()) == h) { gImages.erase(it); break; }
    return kOfxStatOK;
}
static OfxStatus ieClipGetRoD(OfxImageClipHandle, OfxTime, OfxRectD* b) { b->x1 = 0; b->y1 = 0; b->x2 = gW; b->y2 = gH; return kOfxStatOK; }
static int ieAbort(OfxImageEffectHandle) { return 0; }
static OfxStatus ieMemAlloc(OfxImageEffectHandle, size_t n, OfxImageMemoryHandle* m) { *m = reinterpret_cast<OfxImageMemoryHandle>(std::malloc(n)); return *m ? kOfxStatOK : kOfxStatErrMemory; }
static OfxStatus ieMemFree(OfxImageMemoryHandle m) { std::free(reinterpret_cast<void*>(m)); return kOfxStatOK; }
static OfxStatus ieMemLock(OfxImageMemoryHandle m, void** p) { *p = reinterpret_cast<void*>(m); return kOfxStatOK; }
static OfxStatus ieMemUnlock(OfxImageMemoryHandle) { return kOfxStatOK; }
static OfxImageEffectSuiteV1 gIESuite;

// ---- misc suites
static OfxStatus memAlloc(void*, size_t n, void** p) { *p = std::malloc(n); return *p ? kOfxStatOK : kOfxStatErrMemory; }
static OfxStatus memFree(void* p) { std::free(p); return kOfxStatOK; }
static OfxMemorySuiteV1 gMemSuite = {memAlloc, memFree};

static OfxStatus mtRun(OfxThreadFunctionV1 f, unsigned n, void* a) { for (unsigned i = 0; i < n; ++i) f(i, n, a); return kOfxStatOK; }
static OfxStatus mtCPUs(unsigned* n) { *n = 1; return kOfxStatOK; }
static OfxStatus mtIndex(unsigned* i) { *i = 0; return kOfxStatOK; }
static int mtSpawned() { return 0; }
static OfxStatus muCreate(OfxMutexHandle* m, int) { *m = reinterpret_cast<OfxMutexHandle>(new int(0)); return kOfxStatOK; }
static OfxStatus muDestroy(const OfxMutexHandle m) { delete reinterpret_cast<int*>(m); return kOfxStatOK; }
static OfxStatus muNoop(const OfxMutexHandle) { return kOfxStatOK; }
static OfxMultiThreadSuiteV1 gMTSuite = {mtRun, mtCPUs, mtIndex, mtSpawned, muCreate, muDestroy, muNoop, muNoop, muNoop};

static OfxStatus msgMessage(void*, const char* type, const char* id, const char* fmt, ...) {
    char buf[2048]; va_list ap; va_start(ap, fmt); std::vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    gMessages.push_back(std::string(type) + ": " + buf); (void)id;
    return std::strcmp(type, kOfxMessageQuestion) == 0 ? kOfxStatReplyYes : kOfxStatOK;
}
static OfxMessageSuiteV1 gMsgSuite = {msgMessage};

static OfxStatus intRedraw(OfxInteractHandle) { ++gRedraws; return kOfxStatOK; }
static OfxStatus intSwap(OfxInteractHandle) { return kOfxStatOK; }
static OfxStatus intProps(OfxInteractHandle h, OfxPropertySetHandle* out) { *out = reinterpret_cast<OfxPropertySetHandle>(h); return kOfxStatOK; }
static OfxInteractSuiteV1 gIntSuite = {intSwap, intRedraw, intProps};

static OfxStatus dGetColour(OfxDrawContextHandle, OfxStandardColour, OfxRGBAColourF* c) { *c = {1, 1, 1, 1}; return kOfxStatOK; }
static OfxStatus dSetColour(OfxDrawContextHandle, const OfxRGBAColourF*) { return kOfxStatOK; }
static OfxStatus dSetLineWidth(OfxDrawContextHandle, float) { return kOfxStatOK; }
static OfxStatus dSetStipple(OfxDrawContextHandle, OfxDrawLineStipplePattern) { return kOfxStatOK; }
static OfxStatus dDraw(OfxDrawContextHandle, OfxDrawPrimitive, const OfxPointD*, int) { ++gDrawCalls; return kOfxStatOK; }
static OfxStatus dText(OfxDrawContextHandle, const char* t, const OfxPointD*, int) { gDrawText.push_back(t); return kOfxStatOK; }
static OfxDrawSuiteV1 gDrawSuite;

static const void* fetchSuite(OfxPropertySetHandle, const char* name, int ver) {
    std::string n = name;
    if (n == kOfxPropertySuite && ver == 1) return &gPropSuite;
    if (n == kOfxImageEffectSuite && ver == 1) return &gIESuite;
    if (n == kOfxParameterSuite && ver == 1) return &gParamSuite;
    if (n == kOfxMemorySuite && ver == 1) return &gMemSuite;
    if (n == kOfxMultiThreadSuite && ver == 1) return &gMTSuite;
    if (n == kOfxMessageSuite && ver == 1) return &gMsgSuite;
    if (n == kOfxInteractSuite && ver == 1) return &gIntSuite;
    if (n == kOfxDrawSuite && ver == 1) return &gDrawSuite;
    return nullptr;
}

// ------------------------------------------------------------------------------------------------ driver
static OfxPlugin* gPlugin = nullptr;
static void die(const char* m) { std::fprintf(stderr, "HOST FAIL: %s\n", m); std::exit(2); }
static OfxStatus act(const char* action, void* handle, PropSet* in, PropSet* out) {
    return gPlugin->mainEntry(action, handle, reinterpret_cast<OfxPropertySetHandle>(in), reinterpret_cast<OfxPropertySetHandle>(out));
}
static std::string workdir;

static void loadSource() {
    std::string path = workdir + "/src.f32";
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) die("cannot open src.f32");
    int hdr[2];
    if (std::fread(hdr, sizeof(int), 2, f) != 2) die("bad src header");
    gW = hdr[0]; gH = hdr[1];
    gSrcTopDown.resize(size_t(gW) * gH * 4);
    if (std::fread(gSrcTopDown.data(), sizeof(float), gSrcTopDown.size(), f) != gSrcTopDown.size()) die("bad src data");
    std::fclose(f);
    gStoreSrc.resize(gSrcTopDown.size());
    gStoreDst.assign(gSrcTopDown.size(), 0.f);
    // Positive row stride: memory row 0 is the bottom scanline (OFX convention).
    // Negative row stride: memory is top-down; `data` points at the bottom scanline (last row) and rows step backwards.
    for (int y = 0; y < gH; ++y) {
        const float* s = &gSrcTopDown[size_t(y) * gW * 4];
        int memRow = gNegRowBytes ? y : gH - 1 - y;
        std::memcpy(&gStoreSrc[size_t(memRow) * gW * 4], s, sizeof(float) * gW * 4);
    }
}

static Effect* newContextDescriptor(const char* ctx, PropSet* pluginDesc) {
    (void)pluginDesc;
    auto* e = new Effect();
    e->context = ctx;
    return e;
}

static Param* P(const char* n) {
    auto it = gInstance->ps.params.find(n);
    if (it == gInstance->ps.params.end()) { std::fprintf(stderr, "HOST FAIL: no param %s\n", n); std::exit(2); }
    return it->second.get();
}

static void setParam(const std::string& name, const std::string& val) {
    Param* p = P(name.c_str());
    if (p->type == kOfxParamTypeString || p->type == kOfxParamTypeCustom) p->sv = val;
    else if (p->type == kOfxParamTypeDouble) p->dv = {std::atof(val.c_str())};
    else if (p->type == kOfxParamTypeRGB) {
        double r, g, b; if (std::sscanf(val.c_str(), "%lf,%lf,%lf", &r, &g, &b) == 3) p->dv = {r, g, b};
    } else p->iv = {std::atoi(val.c_str())};
}

static std::string getParamStr(const std::string& name) {
    Param* p = P(name.c_str());
    char b[128];
    if (p->type == kOfxParamTypeString || p->type == kOfxParamTypeCustom) return p->sv;
    if (p->type == kOfxParamTypeDouble) { std::snprintf(b, sizeof(b), "%g", p->dv.empty() ? 0 : p->dv[0]); return b; }
    std::snprintf(b, sizeof(b), "%d", p->iv.empty() ? 0 : p->iv[0]);
    return b;
}

static PropSet* renderArgs(double t) {
    auto* in = new PropSet();
    auto* h = reinterpret_cast<OfxPropertySetHandle>(in);
    pSetD(h, kOfxPropTime, 0, t);
    double rs[2] = {1, 1}; pSetDN(h, kOfxImageEffectPropRenderScale, 2, rs);
    int rw[4] = {0, 0, gW, gH}; pSetIN(h, kOfxImageEffectPropRenderWindow, 4, rw);
    pSetS(h, kOfxImageEffectPropFieldToRender, 0, kOfxImageFieldNone);
    pSetI(h, kOfxImageEffectPropSequentialRenderStatus, 0, 0);
    pSetI(h, kOfxImageEffectPropInteractiveRenderStatus, 0, 0);
    pSetI(h, kOfxImageEffectPropRenderQualityDraft, 0, 0);
    return in;
}

int main(int argc, char** argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: mock_host plugin.ofx workdir commands...\n"); return 1; }
    gNegRowBytes = std::getenv("MOCK_NEGATIVE_ROWBYTES") != nullptr;
    workdir = argv[2];
    loadSource();

    std::memset(&gIESuite, 0, sizeof(gIESuite));
    gIESuite.getPropertySet = ieGetPropertySet; gIESuite.getParamSet = ieGetParamSet; gIESuite.clipDefine = ieClipDefine;
    gIESuite.clipGetHandle = ieClipGetHandle; gIESuite.clipGetPropertySet = ieClipGetPropertySet; gIESuite.clipGetImage = ieClipGetImage;
    gIESuite.clipReleaseImage = ieClipReleaseImage; gIESuite.clipGetRegionOfDefinition = ieClipGetRoD; gIESuite.abort = ieAbort;
    gIESuite.imageMemoryAlloc = ieMemAlloc; gIESuite.imageMemoryFree = ieMemFree; gIESuite.imageMemoryLock = ieMemLock; gIESuite.imageMemoryUnlock = ieMemUnlock;
    initParamSuite();
    std::memset(&gDrawSuite, 0, sizeof(gDrawSuite));
    gDrawSuite.getColour = dGetColour; gDrawSuite.setColour = dSetColour; gDrawSuite.setLineWidth = dSetLineWidth;
    gDrawSuite.setLineStipple = dSetStipple; gDrawSuite.draw = dDraw; gDrawSuite.drawText = dText;

    HMODULE mod = LoadLibraryA(argv[1]);
    if (!mod) { std::fprintf(stderr, "HOST FAIL: LoadLibrary %s error %lu\n", argv[1], GetLastError()); return 2; }
    auto getN = reinterpret_cast<int (*)()>(GetProcAddress(mod, "OfxGetNumberOfPlugins"));
    auto getP = reinterpret_cast<OfxPlugin* (*)(int)>(GetProcAddress(mod, "OfxGetPlugin"));
    if (!getN || !getP) die("missing OfxGetNumberOfPlugins/OfxGetPlugin");
    std::printf("plugins=%d\n", getN());
    gPlugin = getP(0);
    std::printf("id=%s v%u.%u api=%s\n", gPlugin->pluginIdentifier, gPlugin->pluginVersionMajor, gPlugin->pluginVersionMinor, gPlugin->pluginApi);

    // host description
    static PropSet hostProps; static OfxHost host{reinterpret_cast<OfxPropertySetHandle>(&hostProps), fetchSuite};
    auto* hp = reinterpret_cast<OfxPropertySetHandle>(&hostProps);
    pSetS(hp, kOfxPropType, 0, "OfxTypeImageEffectHost");
    pSetS(hp, kOfxPropName, 0, "mock-host"); pSetS(hp, kOfxPropLabel, 0, "Mock Host");
    pSetI(hp, kOfxImageEffectHostPropIsBackground, 0, 0);
    pSetI(hp, kOfxImageEffectPropSupportsOverlays, 0, 1);
    pSetI(hp, kOfxImageEffectPropSupportsMultiResolution, 0, 0);
    pSetI(hp, kOfxImageEffectPropSupportsTiles, 0, 0);
    pSetI(hp, kOfxImageEffectPropTemporalClipAccess, 0, 0);
    pSetS(hp, kOfxImageEffectPropSupportedComponents, 0, kOfxImageComponentRGBA);
    pSetS(hp, kOfxImageEffectPropSupportedContexts, 0, kOfxImageEffectContextFilter);
    pSetS(hp, kOfxImageEffectPropSupportedContexts, 1, kOfxImageEffectContextGeneral);
    pSetS(hp, kOfxImageEffectPropSupportedPixelDepths, 0, kOfxBitDepthFloat);
    pSetS(hp, kOfxImageEffectPropSupportedPixelDepths, 1, kOfxBitDepthByte);
    pSetI(hp, kOfxImageEffectPropSupportsMultipleClipDepths, 0, 0);
    pSetI(hp, kOfxImageEffectPropSupportsMultipleClipPARs, 0, 0);
    pSetI(hp, kOfxImageEffectPropSetableFrameRate, 0, 0);
    pSetI(hp, kOfxImageEffectPropSetableFielding, 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsStringAnimation, 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsCustomInteract, 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsChoiceAnimation, 0, 0);
    pSetI(hp, "OfxParamHostPropSupportsStrChoiceAnimation", 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsBooleanAnimation, 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsCustomAnimation, 0, 0);
    pSetI(hp, "OfxParamHostPropSupportsParametricAnimation", 0, 0);
    pSetI(hp, kOfxParamHostPropMaxParameters, 0, -1);
    pSetI(hp, kOfxParamHostPropMaxPages, 0, 0);
    pSetI(hp, kOfxParamHostPropSupportsCustomInteract, 0, 0);
    int pageSize[2] = {0, 0}; pSetIN(hp, kOfxParamHostPropPageRowColumnCount, 2, pageSize);
    pSetI(hp, kOfxPropAPIVersion, 0, 1); pSetI(hp, kOfxPropAPIVersion, 1, 4);
    gPlugin->setHost(&host);

    Effect* plugDesc = new Effect();
    plugDesc->context = "";
    { auto st = act(kOfxActionLoad, nullptr, nullptr, nullptr); if (st != kOfxStatOK) { std::fprintf(stderr, "HOST: Load status %d\n", st); die("Load failed"); } }
    { auto st = act(kOfxActionDescribe, plugDesc, nullptr, nullptr); if (st != kOfxStatOK) { std::fprintf(stderr, "HOST: Describe status %d\n", st); die("Describe failed"); } }
    std::printf("describe=OK\n");

    // contexts the plugin declared
    Prop& ctxs = plugDesc->props.m[kOfxImageEffectPropSupportedContexts];
    std::string ctxUsed;
    for (auto& c : ctxs.s) {
        Effect* ce = newContextDescriptor(c.c_str(), &plugDesc->props);
        ce->props = plugDesc->props;      // context descriptor starts from the plugin descriptor
        auto* in = new PropSet(); pSetS(reinterpret_cast<OfxPropertySetHandle>(in), kOfxImageEffectPropContext, 0, c.c_str());
        auto st = act(kOfxImageEffectActionDescribeInContext, ce, in, nullptr);
        std::printf("describeInContext[%s]=%s clips=%zu params=%zu\n", c.c_str(), st == kOfxStatOK ? "OK" : "FAIL", ce->clips.size(), ce->ps.params.size());
        if (st != kOfxStatOK) { std::fprintf(stderr, "HOST: DescribeInContext status %d\n", st); die("DescribeInContext failed"); }
        if (ctxUsed.empty() || c == kOfxImageEffectContextFilter) { if (ctxUsed.empty()) gInstance = ce; }
        if (c == kOfxImageEffectContextFilter) { gInstance = ce; ctxUsed = c; }
        if (ctxUsed.empty()) ctxUsed = c;
    }
    if (!gInstance) die("no context described");

    // instantiate: derive instance state from the descriptor
    for (auto& kv : gInstance->ps.params) {
        Param* p = kv.second.get();
        Prop* def = nullptr;
        auto it = p->props.m.find(kOfxParamPropDefault);
        if (it != p->props.m.end()) def = &it->second;
        if (p->type == kOfxParamTypeString || p->type == kOfxParamTypeCustom) p->sv = (def && !def->s.empty()) ? def->s[0] : "";
        else if (p->type == kOfxParamTypeDouble) p->dv = {(def && !def->d.empty()) ? def->d[0] : 0.0};
        else if (p->type == kOfxParamTypeRGB) { p->dv.assign(3, 0.0); if (def) for (size_t k = 0; k < def->d.size() && k < 3; ++k) p->dv[k] = def->d[k]; }
        else if (p->type == kOfxParamTypeRGBA) { p->dv.assign(4, 0.0); if (def) for (size_t k = 0; k < def->d.size() && k < 4; ++k) p->dv[k] = def->d[k]; }
        else if (p->type == kOfxParamTypePushButton || p->type == kOfxParamTypeGroup || p->type == kOfxParamTypePage) {}
        else p->iv = {(def && !def->i.empty()) ? def->i[0] : 0};
    }
    for (auto& kv : gInstance->clips) {
        auto* h = reinterpret_cast<OfxPropertySetHandle>(&kv.second->props);
        pSetI(h, kOfxImageClipPropConnected, 0, kv.first == "Background" ? 0 : 1);
        pSetS(h, kOfxImageEffectPropPixelDepth, 0, kOfxBitDepthFloat);
        pSetS(h, kOfxImageEffectPropComponents, 0, kOfxImageComponentRGBA);
        pSetS(h, kOfxImageClipPropUnmappedComponents, 0, kOfxImageComponentRGBA);
        pSetS(h, kOfxImageEffectPropPreMultiplication, 0, kOfxImageUnPreMultiplied);
        pSetD(h, kOfxImagePropPixelAspectRatio, 0, 1.0);
        pSetD(h, kOfxImageEffectPropFrameRate, 0, 24.0);
        double fr[2] = {0, 99}; pSetDN(h, kOfxImageEffectPropFrameRange, 2, fr);
        double ufr[2] = {0, 99}; pSetDN(h, kOfxImageEffectPropUnmappedFrameRange, 2, ufr);
        pSetS(h, kOfxImageClipPropFieldOrder, 0, kOfxImageFieldNone);
    }
    auto* ip = reinterpret_cast<OfxPropertySetHandle>(&gInstance->props);
    pSetS(ip, kOfxImageEffectPropContext, 0, ctxUsed.c_str());
    pSetP(ip, kOfxPropInstanceData, 0, nullptr);
    double size[2] = {double(gW), double(gH)}; double zero[2] = {0, 0};
    pSetDN(ip, kOfxImageEffectPropProjectSize, 2, size); pSetDN(ip, kOfxImageEffectPropProjectOffset, 2, zero);
    pSetDN(ip, kOfxImageEffectPropProjectExtent, 2, size); pSetD(ip, kOfxImageEffectPropProjectPixelAspectRatio, 0, 1.0);
    pSetD(ip, kOfxImageEffectInstancePropEffectDuration, 0, 100.0);
    pSetI(ip, kOfxImageEffectInstancePropSequentialRender, 0, 0);
    pSetI(ip, kOfxPropIsInteractive, 0, 1);
    pSetD(ip, kOfxImageEffectPropFrameRate, 0, 24.0);
    { auto st = act(kOfxActionCreateInstance, gInstance, nullptr, nullptr); if (st != kOfxStatOK) { std::fprintf(stderr, "HOST: CreateInstance status %d\n", st); die("CreateInstance failed"); } }
    std::printf("createInstance=OK\n");
    {
        auto* in = new PropSet(); auto* out = new PropSet();
        auto st = act(kOfxImageEffectActionGetClipPreferences, gInstance, in, out);
        std::printf("getClipPreferences=%s\n", st == kOfxStatOK || st == kOfxStatReplyDefault ? "OK" : "FAIL");
        auto it = out->m.find(kOfxImageEffectPropPreMultiplication);
        if (it != out->m.end() && !it->second.s.empty()) std::printf("outputPremult=%s\n", it->second.s[0].c_str());
    }

    // overlay interact (optional)
    OfxPluginEntryPoint* overlayEntry = nullptr;
    { auto it = plugDesc->props.m.find(kOfxImageEffectPluginPropOverlayInteractV2); if (it != plugDesc->props.m.end() && !it->second.p.empty()) overlayEntry = reinterpret_cast<OfxPluginEntryPoint*>(it->second.p[0]); }
    PropSet interactProps;
    OfxPropertySetHandle iph = reinterpret_cast<OfxPropertySetHandle>(&interactProps);
    auto interact = [&](const char* action, PropSet* in) -> OfxStatus {
        return overlayEntry(action, reinterpret_cast<void*>(&interactProps), reinterpret_cast<OfxPropertySetHandle>(in), nullptr);
    };
    bool overlayReady = false;
    if (overlayEntry) {
        pSetP(iph, kOfxPropEffectInstance, 0, gInstance);
        double ps[2] = {1, 1}; pSetDN(iph, kOfxInteractPropPixelScale, 2, ps);
        double bg[3] = {0, 0, 0}; pSetDN(iph, kOfxInteractPropBackgroundColour, 3, bg);
        auto st1 = overlayEntry(kOfxActionDescribe, reinterpret_cast<void*>(&interactProps), nullptr, nullptr);
        auto st2 = overlayEntry(kOfxActionCreateInstance, reinterpret_cast<void*>(&interactProps), nullptr, nullptr);
        std::printf("overlay describe=%d create=%d\n", st1, st2);
        overlayReady = st2 == kOfxStatOK;
    }
    auto overlayArgs = [&](double t) {
        auto* in = new PropSet(); auto* h = reinterpret_cast<OfxPropertySetHandle>(in);
        pSetP(h, kOfxPropEffectInstance, 0, gInstance); pSetD(h, kOfxPropTime, 0, t);
        double rs[2] = {1, 1}; pSetDN(h, kOfxImageEffectPropRenderScale, 2, rs);
        double ps[2] = {1, 1}; pSetDN(h, kOfxInteractPropPixelScale, 2, ps);
        double bg[3] = {0, 0, 0}; pSetDN(h, kOfxInteractPropBackgroundColour, 3, bg);
        return in;
    };

    for (int a = 3; a < argc; ++a) {
        std::string c = argv[a];
        auto colon = c.find(':');
        std::string verb = c.substr(0, colon), arg = colon == std::string::npos ? "" : c.substr(colon + 1);
        double t = 0;
        auto at = arg.find('@');
        if (at != std::string::npos) { t = std::atof(arg.c_str() + at + 1); arg = arg.substr(0, at); }
        if (verb.find('@') != std::string::npos) { t = std::atof(verb.c_str() + verb.find('@') + 1); verb = verb.substr(0, verb.find('@')); }
        if (verb == "set") {
            auto eq = arg.find('=');
            setParam(arg.substr(0, eq), arg.substr(eq + 1));
        } else if (verb == "get") {
            std::printf("%s=%s\n", arg.c_str(), getParamStr(arg).c_str());
        } else if (verb == "dump") {
            for (auto& n : gInstance->ps.order) std::printf("param:%s:%s\n", n.c_str(), gInstance->ps.params[n]->type.c_str());
        } else if (verb == "wait") {
            Sleep(DWORD(std::atoi(arg.c_str())));
        } else if (verb == "changed") {
            auto* in = new PropSet(); auto* h = reinterpret_cast<OfxPropertySetHandle>(in);
            pSetS(h, kOfxPropType, 0, kOfxTypeParameter); pSetS(h, kOfxPropName, 0, arg.c_str());
            pSetS(h, kOfxPropChangeReason, 0, kOfxChangeUserEdited); pSetD(h, kOfxPropTime, 0, t);
            double rs[2] = {1, 1}; pSetDN(h, kOfxImageEffectPropRenderScale, 2, rs);
            auto st = act(kOfxActionInstanceChanged, gInstance, in, nullptr);
            std::printf("changed:%s=%d\n", arg.c_str(), st);
        } else if (verb == "identity") {
            auto* in = renderArgs(t); auto* out = new PropSet();
            auto st = act(kOfxImageEffectActionIsIdentity, gInstance, in, out);
            auto it = out->m.find(kOfxPropName);
            std::printf("identity=%s clip=%s\n", st == kOfxStatOK ? "yes" : "no", it != out->m.end() && !it->second.s.empty() ? it->second.s[0].c_str() : "-");
        } else if (verb == "render") {
            std::fill(gStoreDst.begin(), gStoreDst.end(), -7.f);      // sentinel: detect unwritten pixels
            auto* in = renderArgs(t);
            auto st = act(kOfxImageEffectActionBeginSequenceRender, gInstance, in, nullptr);
            (void)st;
            st = act(kOfxImageEffectActionRender, gInstance, in, nullptr);
            act(kOfxImageEffectActionEndSequenceRender, gInstance, in, nullptr);
            std::printf("render=%d\n", st);
            if (st != kOfxStatOK) die("render failed");
            std::vector<float> top(gStoreDst.size());
            for (int y = 0; y < gH; ++y) std::memcpy(&top[size_t(y) * gW * 4], &gStoreDst[size_t(gNegRowBytes ? y : gH - 1 - y) * gW * 4], sizeof(float) * gW * 4);
            FILE* f = std::fopen((workdir + "/" + arg + ".f32").c_str(), "wb");
            int hdr[2] = {gW, gH}; std::fwrite(hdr, sizeof(int), 2, f); std::fwrite(top.data(), sizeof(float), top.size(), f); std::fclose(f);
        } else if (verb == "draw" && overlayReady) {
            gDrawText.clear(); gDrawCalls = 0;
            auto* in = overlayArgs(t);
            pSetP(reinterpret_cast<OfxPropertySetHandle>(in), kOfxInteractPropDrawContext, 0, reinterpret_cast<void*>(0x1));
            auto st = interact(kOfxInteractActionDraw, in);
            std::printf("draw=%d calls=%d\n", st, gDrawCalls);
            for (auto& s : gDrawText) std::printf("text:%s\n", s.c_str());
        } else if (verb == "pen" && overlayReady) {
            double x = 0, y = 0; std::sscanf(arg.c_str(), "%lf,%lf", &x, &y);
            for (const char* action : {kOfxInteractActionPenDown, kOfxInteractActionPenUp}) {
                auto* in = overlayArgs(t); auto* h = reinterpret_cast<OfxPropertySetHandle>(in);
                double pp[2] = {x, y}; pSetDN(h, kOfxInteractPropPenPosition, 2, pp);
                int vp[2] = {int(x), int(y)}; pSetIN(h, kOfxInteractPropPenViewportPosition, 2, vp);
                pSetD(h, kOfxInteractPropPenPressure, 0, 1.0);
                auto st = interact(action, in);
                std::printf("%s=%d\n", action, st);
            }
        }
    }
    for (auto& m : gMessages) std::printf("message:%s\n", m.c_str());
    act(kOfxActionDestroyInstance, gInstance, nullptr, nullptr);
    act(kOfxActionUnload, nullptr, nullptr, nullptr);
    std::printf("done\n");
    return 0;
}
