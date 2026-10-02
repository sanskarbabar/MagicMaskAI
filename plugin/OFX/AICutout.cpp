// AI Cutout — OFX plugin for DaVinci Resolve.
//
// Deliberately small. All the work (pick the subject, track, correct) happens in the AI Cutout app; this plugin only
// shows the finished matte on a clip: it reads the per-frame mattes the app wrote to the cache and outputs the clip with
// a real alpha channel (or a matte / overlay / checkerboard view). It has no ML runtime, no threads and no network code,
// and rendering never waits for anything.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <list>
#include <memory>
#include <mutex>
#include <string>

#include <sys/stat.h>

#include "ofxsImageEffect.h"

#include "acm.h"
#include "edge.h"
#include "util.h"

using namespace OFX;

#define kPluginName "AI Cutout"
#define kPluginIdentifier "com.aicutout.AICutout"
#define kPluginDescription "Shows the subject cut out by the AI Cutout app. Press 'Open AI Cutout', pick the subject, track and " \
                           "render, then come back and press 'Update Matte'. Output 'Cutout' gives the clip a real alpha channel."

namespace {

enum { kOutCutout = 0, kOutMatte, kOutOverlay, kOutChecker, kOutOriginal };

int choiceAt(ChoiceParam* p, double t) { int v = 0; p->getValueAtTime(t, v); return v; }

// Recently used mattes (a frame is re-rendered many times while scrubbing).
class MatteCache {
public:
    std::shared_ptr<acut::Matte> get(const std::string& setDir, int idx) {
        std::string path = acut::framePath(setDir, idx);
        struct _stat64 st;
        if (_stat64(path.c_str(), &st) != 0) return nullptr;
        char key[64];
        std::snprintf(key, sizeof(key), "|%lld|%lld", (long long)st.st_size, (long long)st.st_mtime);
        std::string fullKey = path + key;
        {
            std::lock_guard<std::mutex> lk(m_);
            for (auto it = list_.begin(); it != list_.end(); ++it)
                if (it->first == fullKey) { list_.splice(list_.begin(), list_, it); return it->second; }
        }
        auto mt = std::make_shared<acut::Matte>();
        if (!acut::readFrameFile(path, *mt)) { acut::logf("unreadable matte: %s", path.c_str()); return nullptr; }
        std::lock_guard<std::mutex> lk(m_);
        list_.emplace_front(fullKey, mt);
        while (list_.size() > 8) list_.pop_back();
        return mt;
    }
private:
    std::mutex m_;
    std::list<std::pair<std::string, std::shared_ptr<acut::Matte>>> list_;
};
MatteCache gMattes;

}  // namespace

class AICutoutPlugin : public ImageEffect {
public:
    explicit AICutoutPlugin(OfxImageEffectHandle h) : ImageEffect(h) {
        dst_ = fetchClip(kOfxImageEffectOutputClipName);
        src_ = fetchClip(kOfxImageEffectSimpleSourceClipName);
        output_ = fetchChoiceParam("output");
        feather_ = fetchDoubleParam("feather");
        shift_ = fetchDoubleParam("edgeShift");
        clean_ = fetchBooleanParam("cleanEdge");
        offset_ = fetchIntParam("frameOffset");
        matteSet_ = fetchStringParam("matteSet");
        revision_ = fetchIntParam("revision");
        status_ = fetchStringParam("status");
    }

    int frameIndex(double t) const {
        long first = 0;
        try { first = std::lround(src_->getFrameRange().min); } catch (...) {}
        return int(std::lround(t) - first + offset_->getValue());
    }

    // The matte set to render: the pinned one, or (empty) the most recently analysed clip.
    std::string matteDir() {
        std::string name;
        matteSet_->getValue(name);
        if (name.empty()) name = acut::readActiveSet(acut::cacheRoot());
        return name.empty() ? "" : acut::cacheRoot() + "/" + name;
    }

    void say(const std::string& s) { status_->setValue(s); }

    void changedParam(const InstanceChangedArgs&, const std::string& name) override {
        try {
            if (name == "openApp") {
                std::string err;
                if (acut::launchApp(err)) say("AI Cutout is open. When you have rendered, press 'Update Matte'.");
                else { say(err); sendMessage(Message::eMessageError, "", err); }
            } else if (name == "update") {
                std::string latest = acut::readActiveSet(acut::cacheRoot());
                if (latest.empty()) {
                    say("No matte yet. Press 'Open AI Cutout', pick the subject, track, then come back.");
                } else {
                    matteSet_->setValue(latest);                         // pin this clip to the matte it was just given
                    acut::MatteMeta m = acut::readMeta(acut::cacheRoot() + "/" + latest);
                    say(m.ok ? "Matte linked (" + std::to_string(m.frames) + " frames)." : "Matte linked.");
                }
                revision_->setValue(revision_->getValue() + 1);          // tells Resolve to re-render
            }
        } catch (const std::exception& e) {
            acut::logf("changedParam(%s): %s", name.c_str(), e.what());
        }
    }

    bool isIdentity(const IsIdentityArguments& args, Clip*& clip, double& time) override {
        if (choiceAt(output_, args.time) == kOutOriginal) { clip = src_; time = args.time; return true; }
        return false;
    }

    void getClipPreferences(ClipPreferencesSetter& prefs) override {
        prefs.setClipComponents(*dst_, ePixelComponentRGBA);
        prefs.setOutputPremultiplication(eImageUnPreMultiplied);
    }

    void render(const RenderArguments& args) override {
        std::unique_ptr<Image> dst(dst_->fetchImage(args.time));
        std::unique_ptr<Image> src(src_->fetchImage(args.time));
        if (!dst || !src) throwSuiteStatusException(kOfxStatFailed);
        if (dst->getPixelDepth() != eBitDepthFloat || dst->getPixelComponents() != ePixelComponentRGBA ||
            src->getPixelDepth() != eBitDepthFloat || src->getPixelComponents() != ePixelComponentRGBA)
            throwSuiteStatusException(kOfxStatErrFormat);

        const OfxRectI sb = src->getBounds(), db = dst->getBounds();
        acut::RGBAImage S, D;
        S.w = sb.x2 - sb.x1; S.h = sb.y2 - sb.y1; S.data = static_cast<float*>(src->getPixelAddress(sb.x1, sb.y1));
        S.stride = ptrdiff_t(src->getRowBytes()) / ptrdiff_t(sizeof(float));
        D.w = db.x2 - db.x1; D.h = db.y2 - db.y1; D.data = static_cast<float*>(dst->getPixelAddress(db.x1, db.y1));
        D.stride = ptrdiff_t(dst->getRowBytes()) / ptrdiff_t(sizeof(float));
        if (!S.data || !D.data) throwSuiteStatusException(kOfxStatFailed);

        acut::RenderParams rp;
        switch (choiceAt(output_, args.time)) {
            case kOutMatte: rp.mode = acut::kAlpha; break;
            case kOutOverlay: rp.mode = acut::kOverlay; break;
            case kOutChecker: rp.mode = acut::kCheckerboard; break;
            case kOutOriginal: rp.mode = acut::kOriginal; break;
            default: rp.mode = acut::kCutout; break;
        }
        rp.matteFlipY = true;                                   // OFX rows are bottom-up, mattes are top-down
        rp.renderScale = float(args.renderScale.x);
        rp.edge.edgeShift = float(shift_->getValueAtTime(args.time));
        rp.edge.feather = float(feather_->getValueAtTime(args.time));
        rp.edge.refine = 0.5f;                                  // fixed, good defaults (see docs/BENCHMARKS.md)
        rp.edge.quality = 1;
        if (clean_->getValueAtTime(args.time)) { rp.edge.decontaminate = 0.6f; rp.edge.spill = 0.3f; }

        std::shared_ptr<acut::Matte> matte;
        std::string dir = matteDir();
        if (!dir.empty()) matte = gMattes.get(dir, frameIndex(args.time));

        try {
            if (S.w == D.w && S.h == D.h) acut::render(S, matte.get(), rp, D);
            else copyOverlap(S, D);
        } catch (const std::exception& e) {
            acut::logf("render: %s", e.what());
            copyOverlap(S, D);                                  // never fail the frame: show the source
        }
    }

private:
    static void copyOverlap(const acut::RGBAImage& S, acut::RGBAImage& D) {
        const int w = std::min(S.w, D.w), h = std::min(S.h, D.h);
        for (int y = 0; y < h; ++y) {
            const float* s = S.px(0, y); float* d = D.px(0, y);
            for (int x = 0; x < w * 4; ++x) d[x] = s[x];
        }
    }

    Clip *dst_ = nullptr, *src_ = nullptr;
    ChoiceParam* output_;
    DoubleParam *feather_, *shift_;
    BooleanParam* clean_;
    IntParam *offset_, *revision_;
    StringParam *matteSet_, *status_;
};

class AICutoutFactory : public PluginFactoryHelper<AICutoutFactory> {
public:
    AICutoutFactory() : PluginFactoryHelper<AICutoutFactory>(kPluginIdentifier, 0, 2) {}
    void describe(ImageEffectDescriptor& d) override {
        d.setLabels(kPluginName, kPluginName, kPluginName);
        d.setPluginGrouping("AI Cutout");
        d.setPluginDescription(kPluginDescription);
        d.addSupportedContext(eContextFilter);
        d.addSupportedContext(eContextGeneral);
        d.addSupportedBitDepth(eBitDepthFloat);
        d.setSingleInstance(false);
        d.setHostFrameThreading(false);
        d.setSupportsMultiResolution(false);
        d.setSupportsTiles(false);
        d.setTemporalClipAccess(false);
        d.setRenderTwiceAlways(false);
        d.setSupportsMultipleClipPARs(false);
        d.setRenderThreadSafety(eRenderFullySafe);
        d.setSupportsCudaRender(false);        // CPU render; see docs/BENCHMARKS.md
        d.setSupportsOpenCLRender(false);
    }

    void describeInContext(ImageEffectDescriptor& d, ContextEnum) override {
        ClipDescriptor* src = d.defineClip(kOfxImageEffectSimpleSourceClipName);
        src->addSupportedComponent(ePixelComponentRGBA);
        src->setTemporalClipAccess(false);
        src->setSupportsTiles(false);
        ClipDescriptor* dst = d.defineClip(kOfxImageEffectOutputClipName);
        dst->addSupportedComponent(ePixelComponentRGBA);
        dst->setSupportsTiles(false);

        PageParamDescriptor* page = d.definePageParam("Controls");
        auto add = [&](ParamDescriptor* p) { page->addChild(*p); };

        PushButtonParamDescriptor* open = d.definePushButtonParam("openApp");
        open->setLabel("Open AI Cutout");
        open->setHint("Opens the AI Cutout app: pick the subject once, it tracks the whole clip.");
        add(open);
        PushButtonParamDescriptor* upd = d.definePushButtonParam("update");
        upd->setLabel("Update Matte");
        upd->setHint("After rendering in the app, press this so this clip shows the new matte.");
        add(upd);

        ChoiceParamDescriptor* out = d.defineChoiceParam("output");
        out->setLabel("Output");
        out->setHint("Cutout = the clip with a real alpha channel. The others are for checking the result.");
        for (const char* o : {"Cutout (alpha)", "Matte", "Overlay", "Checkerboard", "Original"}) out->appendOption(o);
        out->setDefault(kOutCutout);
        out->setAnimates(false);
        add(out);

        auto dbl = [&](const char* name, const char* label, const char* hint, double lo, double hi) {
            DoubleParamDescriptor* p = d.defineDoubleParam(name);
            p->setLabel(label); p->setHint(hint); p->setDefault(0); p->setRange(lo, hi); p->setDisplayRange(lo, hi);
            p->setIncrement(0.5); p->setDoubleType(eDoubleTypePlain);
            add(p);
        };
        dbl("feather", "Feather", "Softens the edge (pixels).", 0, 50);
        dbl("edgeShift", "Edge Shift", "Grow (+) or shrink (-) the cutout (pixels).", -50, 50);
        BooleanParamDescriptor* clean = d.defineBooleanParam("cleanEdge");
        clean->setLabel("Clean Edge Colors"); clean->setHint("Removes the old background's color from the edge."); clean->setDefault(true);
        clean->setAnimates(false);
        add(clean);

        IntParamDescriptor* off = d.defineIntParam("frameOffset");
        off->setLabel("Frame Offset");
        off->setHint("Only for trimmed clips: the first frame of the original file that this clip uses.");
        off->setDefault(0); off->setRange(-100000, 100000); off->setDisplayRange(-1000, 1000); off->setAnimates(false);
        add(off);

        StringParamDescriptor* st = d.defineStringParam("status");
        st->setLabel("Status"); st->setStringType(eStringTypeSingleLine); st->setDefault("Press 'Open AI Cutout' to begin.");
        st->setEnabled(false); st->setIsPersistant(false); st->setEvaluateOnChange(false); st->setAnimates(false);
        add(st);

        // hidden persistent state
        StringParamDescriptor* ms = d.defineStringParam("matteSet");
        ms->setLabel("Matte Set"); ms->setStringType(eStringTypeSingleLine); ms->setDefault("");
        ms->setIsSecret(true); ms->setAnimates(false); ms->setEvaluateOnChange(true);
        add(ms);
        IntParamDescriptor* rev = d.defineIntParam("revision");
        rev->setLabel("Revision"); rev->setDefault(0); rev->setIsSecret(true); rev->setAnimates(false); rev->setEvaluateOnChange(true);
        add(rev);
    }

    ImageEffect* createInstance(OfxImageEffectHandle h, ContextEnum) override { return new AICutoutPlugin(h); }
};

void OFX::Plugin::getPluginIDs(OFX::PluginFactoryArray& ids) {
    static AICutoutFactory factory;
    ids.push_back(&factory);
}
