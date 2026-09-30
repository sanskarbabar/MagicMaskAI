// AI Cutout — OFX image effect for DaVinci Resolve.
//
// This DLL contains no ML runtime. It (1) exposes the parameters/overlay, (2) renders from matte files that the
// local AI service has already computed (render never waits for AI), and (3) starts background jobs that talk to
// the service over loopback. See ARCHITECTURE.md.
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <windows.h>
#include <sys/stat.h>

#include "ofxsImageEffect.h"
#include "ofxsInteract.h"
#include "ofxsSupportPrivate.h"
#include "ofxDrawSuite.h"

#include "acm.h"
#include "edge.h"
#include "service_link.h"

using namespace OFX;

#define kPluginName "AI Cutout"
#define kPluginGrouping "AI Cutout"
#define kPluginDescription "AI-powered video subject isolation and tracking. Select a subject, analyze, track, and output a " \
                           "cutout with a real alpha channel. Analysis runs in a local service; nothing leaves your computer."
#define kPluginIdentifier "com.aicutout.AICutout"
#define kPluginVersionMajor 0
#define kPluginVersionMinor 1

namespace {

int choiceNow(ChoiceParam* p) { int v = 0; p->getValue(v); return v; }
int choiceAt(ChoiceParam* p, double t) { int v = 0; p->getValueAtTime(t, v); return v; }

// ------------------------------------------------------------------------------------------------ enums
enum { kModePerson = 0, kModeObject, kModeFace, kModeCustom };
enum { kToolOff = 0, kToolAdd, kToolRemove };
enum { kPaintOff = 0, kPaintAdd, kPaintRemove };
enum { kQDraft = 0, kQBalanced, kQHigh };
enum { kOutMask = 0, kOutAlpha, kOutCutout, kOutComposite };
enum { kPrevOff = 0, kPrevOriginal, kPrevMask, kPrevAlpha, kPrevChecker, kPrevOverlay, kPrevCutout };

const char* kModeNames[] = {"person", "object", "face", "custom"};
const char* kTierNames[] = {"draft", "balanced", "high"};

// ------------------------------------------------------------------------------------------------ matte cache
struct CachedMatte { std::string key; std::shared_ptr<acut::Matte> m; };

class MatteCache {
public:
    // Returns null when the frame is not (validly) cached.
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
                if (it->key == fullKey) { list_.splice(list_.begin(), list_, it); return it->m; }
        }
        auto mt = std::make_shared<acut::Matte>();
        if (!acut::readFrameFile(path, *mt)) { acut::logf("corrupt or unreadable matte: %s", path.c_str()); return nullptr; }
        std::lock_guard<std::mutex> lk(m_);
        list_.push_front({fullKey, mt});
        while (list_.size() > 10) list_.pop_back();
        return mt;
    }
private:
    std::mutex m_;
    std::list<CachedMatte> list_;
};
MatteCache gMatteCache;

// ------------------------------------------------------------------------------------------------ job state
struct JobState {
    std::mutex m;
    std::string state = "Ready", frame = "-", tracking = "-", conf = "-", eta = "-", message;
    double progress = 0.0;
    bool running = false;
    bool finished = false;            // set by worker; consumed by the UI thread to bump the render revision
    std::string setName;              // matte set reported by the service
    std::atomic<bool> cancel{false};
};

std::string fmtEta(double s) {
    if (s < 0 || s > 86400) return "-";
    int t = int(s + 0.5);
    char b[32];
    if (t >= 3600) std::snprintf(b, sizeof(b), "%d:%02d:%02d", t / 3600, (t / 60) % 60, t % 60);
    else std::snprintf(b, sizeof(b), "%d:%02d", t / 60, t % 60);
    return b;
}

struct Click { int frame; double u, v; int label; };

std::vector<Click> parseClicks(const std::string& s) {
    std::vector<Click> out;
    std::stringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, ';')) {
        Click c;
        if (std::sscanf(tok.c_str(), "%d,%lf,%lf,%d", &c.frame, &c.u, &c.v, &c.label) == 4) out.push_back(c);
    }
    return out;
}

std::string serializeClicks(const std::vector<Click>& v) {
    std::string s;
    char b[96];
    for (auto& c : v) { std::snprintf(b, sizeof(b), "%d,%.5f,%.5f,%d;", c.frame, c.u, c.v, c.label); s += b; }
    return s;
}

// ------------------------------------------------------------------------------------------------ worker (no OFX calls)
struct JobRequest {
    std::string kind;                 // analyze | track | export
    std::string source, mode, tier, direction, exportDir;
    bool recalc = false;
    int offset = 0;
    std::vector<Click> clicks;
    int startFrame = -1, endFrame = -1;
    double eShift = 0, eFeather = 0, eSmooth = 0, eRefine = 0.5, eDecon = 0, eSpill = 0;
};

void setJob(JobState& js, const std::string& state, const std::string& msg) {
    std::lock_guard<std::mutex> lk(js.m);
    js.state = state; js.message = msg;
}

bool pollUntilIdle(JobState& js, const std::string& label, std::string& err, bool& sawError) {
    for (;;) {
        if (js.cancel) { acut::ServiceLink::call("cancel"); err = "Cancelled."; return false; }
        acut::Reply r = acut::ServiceLink::call("status", "", 4000);
        if (!r.ok) { err = r.error; return false; }
        std::string state, msg, e;
        double done = 0, total = 0, cur = 0, conf = 1, eta = -1;
        bool busy = false;
        acut::jGetString(r.raw, "state", state); acut::jGetString(r.raw, "message", msg);
        acut::jGetNumber(r.raw, "frames_done", done); acut::jGetNumber(r.raw, "frames_total", total);
        acut::jGetNumber(r.raw, "current_frame", cur); acut::jGetNumber(r.raw, "confidence", conf);
        acut::jGetNumber(r.raw, "eta_s", eta);
        acut::jGetBool(r.raw, "busy", busy);
        bool hasErr = acut::jGetString(r.raw, "error", e) && !e.empty();
        {
            std::lock_guard<std::mutex> lk(js.m);
            js.state = label; js.message = msg;
            js.progress = total > 0 ? 100.0 * done / total : 0.0;
            char b[64];
            std::snprintf(b, sizeof(b), "%d / %d", int(cur), int(total));
            js.frame = state == "tracking" ? b : js.frame;
            js.tracking = state;
            std::snprintf(b, sizeof(b), "%.0f%%", conf * 100.0);
            js.conf = b;
            js.eta = fmtEta(eta);
        }
        if (hasErr) { err = e; sawError = true; return false; }
        if (!busy) return true;
        Sleep(350);
    }
}

void runJob(std::shared_ptr<JobState> js, JobRequest rq) {
    auto fail = [&](const std::string& msg) {
        acut::logf("job %s failed: %s", rq.kind.c_str(), msg.c_str());
        std::lock_guard<std::mutex> lk(js->m);
        js->state = "Error"; js->message = msg; js->running = false; js->finished = true;
    };
    try {
        std::string err;
        setJob(*js, "Starting", "Starting the AI service...");
        if (!acut::ServiceLink::ensureRunning(err)) return fail(err);
        {
            std::lock_guard<std::mutex> lk(js->m);
            js->cancel = false;
        }

        // open clip (reuses an already-open identical session)
        std::string fields = "\"video\":" + acut::jstr(rq.source) + ",\"mode\":" + acut::jstr(rq.mode) +
                             ",\"tier\":" + acut::jstr(rq.tier);
        setJob(*js, "Opening", "Preparing the clip...");
        acut::Reply r = acut::ServiceLink::call("open", fields, 15000);
        if (!r.ok) return fail(r.error);
        std::string setName;
        if (acut::jGetString(r.raw, "set", setName)) { std::lock_guard<std::mutex> lk(js->m); js->setName = setName; }
        bool sawErr = false;
        if (!pollUntilIdle(*js, "Opening", err, sawErr)) return fail(err);

        // keyframes from the user's clicks
        if (rq.kind == "analyze" || rq.kind == "track") {
            std::map<int, std::vector<Click>> byFrame;
            for (auto& c : rq.clicks) byFrame[c.frame + rq.offset].push_back(c);
            if (byFrame.empty() && rq.kind == "analyze") byFrame[std::max(0, rq.startFrame + rq.offset)] = {};   // auto prompt
            for (auto& kv : byFrame) {
                std::string pts = "[";
                for (size_t i = 0; i < kv.second.size(); ++i) {
                    char b[96];
                    std::snprintf(b, sizeof(b), "%s[%.5f,%.5f,%d]", i ? "," : "", kv.second[i].u, kv.second[i].v, kv.second[i].label);
                    pts += b;
                }
                pts += "]";
                setJob(*js, "Analyzing", "Segmenting the selected subject on frame " + std::to_string(kv.first) + "...");
                r = acut::ServiceLink::call("commit", "\"idx\":" + std::to_string(kv.first) + ",\"points\":" + pts + ",\"normalized\":true", 60000);
                if (!r.ok) return fail(r.error);
            }
        }
        if (rq.kind == "analyze") {
            std::lock_guard<std::mutex> lk(js->m);
            js->state = "Ready"; js->message = "Analysis complete. Check the result, then press Track."; js->running = false; js->finished = true;
            js->progress = 100;
            return;
        }
        if (rq.kind == "track") {
            std::string f = "\"direction\":" + acut::jstr(rq.direction) + ",\"recalc\":" + (rq.recalc ? "true" : "false");
            // Tracking always runs outward from the keyframes (whole clip); the playhead must not limit the range.
            r = acut::ServiceLink::call("track", f, 15000);
            if (!r.ok) return fail(r.error);
            if (!pollUntilIdle(*js, "Tracking", err, sawErr)) return fail(err);
            std::lock_guard<std::mutex> lk(js->m);
            js->state = "Done"; js->message = "Tracking complete."; js->progress = 100; js->running = false; js->finished = true;
            return;
        }
        if (rq.kind == "export") {
            char eb[256];
            std::snprintf(eb, sizeof(eb), "\"edge\":{\"edge_shift\":%.3f,\"feather\":%.3f,\"smooth\":%.3f,\"refine\":%.3f,\"decontaminate\":%.3f,\"spill\":%.3f}",
                          rq.eShift, rq.eFeather, rq.eSmooth, rq.eRefine, rq.eDecon, rq.eSpill);
            r = acut::ServiceLink::call("export", "\"dir\":" + acut::jstr(rq.exportDir) + "," + eb, 15000);
            if (!r.ok) return fail(r.error);
            if (!pollUntilIdle(*js, "Rendering", err, sawErr)) return fail(err);
            std::string dir; acut::jGetString(r.raw, "dir", dir);
            std::lock_guard<std::mutex> lk(js->m);
            js->state = "Done"; js->message = "Render complete. " + dir; js->progress = 100; js->running = false; js->finished = true;
        }
    } catch (const std::exception& e) {
        fail(std::string("Unexpected failure: ") + e.what());
    }
}

}  // namespace

// ================================================================================================
// Effect instance
// ================================================================================================
class AICutoutPlugin : public ImageEffect {
public:
    explicit AICutoutPlugin(OfxImageEffectHandle h) : ImageEffect(h), job_(std::make_shared<JobState>()) {
        dst_ = fetchClip(kOfxImageEffectOutputClipName);
        src_ = fetchClip(kOfxImageEffectSimpleSourceClipName);
        try { bg_ = (getContext() == eContextGeneral) ? fetchClip("Background") : nullptr; } catch (...) { bg_ = nullptr; }
        mode_ = fetchChoiceParam("mode");
        source_ = fetchStringParam("sourceFile");
        offset_ = fetchIntParam("frameOffset");
        matteSet_ = fetchStringParam("matteSet");
        clicks_ = fetchStringParam("clicks");
        revision_ = fetchIntParam("revision");
        tool_ = fetchChoiceParam("clickTool");
        paint_ = fetchChoiceParam("paintMode");
        brush_ = fetchDoubleParam("brushSize");
        brushFeather_ = fetchDoubleParam("brushFeather");
        refine_ = fetchDoubleParam("edgeRefinement");
        quality_ = fetchChoiceParam("quality");
        feather_ = fetchDoubleParam("feather");
        smooth_ = fetchDoubleParam("smooth");
        shift_ = fetchDoubleParam("edgeShift");
        spill_ = fetchDoubleParam("spillSuppression");
        decon_ = fetchDoubleParam("decontaminateEdge");
        output_ = fetchChoiceParam("output");
        preview_ = fetchChoiceParam("preview");
        bgKind_ = fetchChoiceParam("bgKind");
        bgColor_ = fetchRGBParam("bgColor");
        premult_ = fetchBooleanParam("premultiply");
        overlayOpacity_ = fetchDoubleParam("overlayOpacity");
        exportDir_ = fetchStringParam("exportFolder");
        stState_ = fetchStringParam("stState");
        stFrame_ = fetchStringParam("stFrame");
        stTracking_ = fetchStringParam("stTracking");
        stConf_ = fetchStringParam("stConfidence");
        stEta_ = fetchStringParam("stEta");
        stMsg_ = fetchStringParam("stMessage");
        progress_ = fetchDoubleParam("progress");
    }

    ~AICutoutPlugin() override { job_->cancel = true; }

    // ---------------------------------------------------------------------------- helpers used by the overlay
    int frameIndex(double t) const {
        long first = 0;
        try { first = std::lround(src_->getFrameRange().min); } catch (...) {}
        return int(std::lround(t) - first + offset_->getValue());
    }
    int relFrame(double t) const {          // frame relative to the clip start, without the user offset
        long first = 0;
        try { first = std::lround(src_->getFrameRange().min); } catch (...) {}
        return int(std::lround(t) - first);
    }
    std::string matteDir() {
        std::string name;
        matteSet_->getValue(name);
        if (name.empty()) name = acut::readActiveSet(acut::cacheRoot());
        if (name.empty()) return "";
        return acut::cacheRoot() + "/" + name;
    }
    std::vector<Click> clicks() { std::string s; clicks_->getValue(s); return parseClicks(s); }
    void setClicks(const std::vector<Click>& v) { clicks_->setValue(serializeClicks(v)); }
    void bumpRevision() { revision_->setValue(revision_->getValue() + 1); }
    int offsetValue() { return offset_->getValue(); }
    int toolMode() { return choiceNow(tool_); }
    int paintMode() { return choiceNow(paint_); }
    double brushSize() { return brush_->getValue(); }
    std::shared_ptr<JobState> job() { return job_; }
    Clip* srcClip() { return src_; }

    void refreshStatus() {
        // live text from the job worker, or from the service when idle
        std::string state, frame, trk, conf, eta, msg;
        double prog;
        bool fin;
        {
            std::lock_guard<std::mutex> lk(job_->m);
            state = job_->state; frame = job_->frame; trk = job_->tracking; conf = job_->conf; eta = job_->eta;
            msg = job_->message; prog = job_->progress; fin = job_->finished;
            if (fin) job_->finished = false;
        }
        if (msg.empty()) {
            static double lastPoll = 0;
            double now = double(GetTickCount64()) / 1000.0;
            if (now - lastPoll > 2.0) {
                lastPoll = now;
                acut::Reply h = acut::ServiceLink::call("hello", "", 400);
                if (h.ok) {
                    std::string hw; acut::jGetString(h.raw, "hardware", hw);
                    msg = hw + "\n\nReady.";
                } else msg = "AI service is not running yet. It starts automatically when you press Analyze.";
            } else return;
        }
        stState_->setValue(state);
        stFrame_->setValue(frame);
        stTracking_->setValue(trk);
        stConf_->setValue(conf);
        stEta_->setValue(eta);
        stMsg_->setValue(msg);
        progress_->setValue(prog);
        if (fin) {
            std::string setName;
            { std::lock_guard<std::mutex> lk(job_->m); setName = job_->setName; }
            if (!setName.empty()) matteSet_->setValue(setName);
            bumpRevision();          // makes the host re-render with the new mattes
        }
    }

    void startJob(JobRequest rq, double t) {
        {
            std::lock_guard<std::mutex> lk(job_->m);
            if (job_->running) { job_->message = "A job is already running. Wait for it to finish."; return; }
            job_->running = true; job_->finished = false; job_->progress = 0;
            job_->state = "Starting"; job_->message = "Starting..."; job_->frame = "-"; job_->tracking = "-"; job_->conf = "-"; job_->eta = "-";
        }
        std::string src; source_->getValue(src);
        if (src.empty()) {
            std::lock_guard<std::mutex> lk(job_->m);
            job_->running = false; job_->state = "Error";
            job_->message = "Set 'Source File' to the original media file of this clip first.";
            sendMessage(Message::eMessageError, "", job_->message);
            return;
        }
        rq.source = src;
        rq.mode = kModeNames[std::min(3, std::max(0, choiceNow(mode_)))];
        rq.tier = kTierNames[std::min(2, std::max(0, choiceNow(quality_)))];
        rq.offset = offset_->getValue();
        rq.clicks = clicks();
        if (rq.startFrame < 0) rq.startFrame = relFrame(t);
        exportDir_->getValue(rq.exportDir);
        rq.eShift = shift_->getValue(); rq.eFeather = feather_->getValue(); rq.eSmooth = smooth_->getValue();
        rq.eRefine = refine_->getValue(); rq.eDecon = decon_->getValue(); rq.eSpill = spill_->getValue();
        std::thread(runJob, job_, rq).detach();
    }

    // ---------------------------------------------------------------------------- actions
    void changedParam(const InstanceChangedArgs& args, const std::string& name) override {
        try {
            if (name == "analyze") { JobRequest r; r.kind = "analyze"; startJob(r, args.time); }
            else if (name == "track" || name == "trackBoth") { JobRequest r; r.kind = "track"; r.direction = "both"; startJob(r, args.time); }
            else if (name == "trackForward") { JobRequest r; r.kind = "track"; r.direction = "forward"; startJob(r, args.time); }
            else if (name == "trackBackward") { JobRequest r; r.kind = "track"; r.direction = "backward"; startJob(r, args.time); }
            else if (name == "recalculate") { JobRequest r; r.kind = "track"; r.direction = "both"; r.recalc = true; startJob(r, args.time); }
            else if (name == "propagateCorrection") {
                JobRequest r; r.kind = "track"; r.direction = "both"; r.recalc = true; startJob(r, args.time);
            }
            else if (name == "render") { JobRequest r; r.kind = "export"; startJob(r, args.time); }
            else if (name == "previewButton") {
                preview_->setValue(choiceNow(preview_) == kPrevOff ? kPrevOverlay : kPrevOff);
            }
            else if (name == "addSelection") tool_->setValue(kToolAdd);
            else if (name == "removeSelection") tool_->setValue(kToolRemove);
            else if (name == "clearSelection") { clicks_->setValue(""); acut::ServiceLink::call("clear_points", "", 800); }
            else if (name == "correctFrame") {
                // make the visible mask of this frame a manual keyframe (used after painting / refining)
                acut::ServiceLink::call("confirm", "\"frame\":" + std::to_string(relFrame(args.time)) + ",\"offset\":" + std::to_string(offset_->getValue()), 3000);
                bumpRevision();
            }
            else if (name == "reset") {
                job_->cancel = true;
                clicks_->setValue("");
                matteSet_->setValue("");
                acut::ServiceLink::call("reset", "", 3000);
                { std::lock_guard<std::mutex> lk(job_->m); job_->state = "Ready"; job_->message = "Reset."; job_->progress = 0; job_->running = false; }
                bumpRevision();
            }
            else if (name == "cancelJob") { job_->cancel = true; }
            else if (name == "linkActive") {
                std::string a = acut::readActiveSet(acut::cacheRoot());
                if (!a.empty()) {
                    matteSet_->setValue(a);
                    acut::MatteMeta m = acut::readMeta(acut::cacheRoot() + "/" + a);
                    if (m.ok && !m.source.empty()) source_->setValue(m.source);
                    bumpRevision();
                } else sendMessage(Message::eMessageMessage, "", "No analyzed clip found. Analyze a clip first (or use the AI Cutout Companion).");
            }
            refreshStatus();
        } catch (const std::exception& e) {
            acut::logf("changedParam(%s) exception: %s", name.c_str(), e.what());
        }
    }

    // ---------------------------------------------------------------------------- render
    bool isIdentity(const IsIdentityArguments& args, Clip*& identityClip, double& identityTime) override {
        int pv = choiceAt(preview_, args.time);
        if (pv == kPrevOriginal) { identityClip = src_; identityTime = args.time; return true; }
        return false;
    }

    void getClipPreferences(ClipPreferencesSetter& prefs) override {
        prefs.setClipComponents(*dst_, ePixelComponentRGBA);
        prefs.setOutputPremultiplication(premult_->getValue() ? eImagePreMultiplied : eImageUnPreMultiplied);
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
        int out = choiceAt(output_, args.time);
        int pv = choiceAt(preview_, args.time);
        acut::OutputMode mode = acut::kCutout;
        switch (out) { case kOutMask: mode = acut::kMask; break; case kOutAlpha: mode = acut::kAlpha; break;
                       case kOutCutout: mode = acut::kCutout; break; case kOutComposite: mode = acut::kComposite; break; }
        switch (pv) { case kPrevOriginal: mode = acut::kOriginal; break; case kPrevMask: mode = acut::kMask; break;
                      case kPrevAlpha: mode = acut::kAlpha; break; case kPrevChecker: mode = acut::kCheckerboard; break;
                      case kPrevOverlay: mode = acut::kOverlay; break; case kPrevCutout: mode = acut::kCutout; break; default: break; }
        rp.mode = mode;
        rp.premultiply = premult_->getValueAtTime(args.time);
        rp.matteFlipY = true;                         // OFX rows are bottom-up, mattes are top-down
        rp.renderScale = float(args.renderScale.x);
        rp.edge.edgeShift = float(shift_->getValueAtTime(args.time));
        rp.edge.feather = float(feather_->getValueAtTime(args.time));
        rp.edge.smooth = float(smooth_->getValueAtTime(args.time));
        rp.edge.refine = float(refine_->getValueAtTime(args.time));
        rp.edge.decontaminate = float(decon_->getValueAtTime(args.time));
        rp.edge.spill = float(spill_->getValueAtTime(args.time));
        rp.edge.quality = choiceAt(quality_, args.time);
        rp.overlayOpacity = float(overlayOpacity_->getValueAtTime(args.time));
        int bk = choiceAt(bgKind_, args.time);
        rp.bgKind = bk == 1 ? acut::kBgSolid : (bk == 2 ? acut::kBgClip : acut::kBgChecker);
        double r, g, b; bgColor_->getValueAtTime(args.time, r, g, b);
        rp.bgColor[0] = float(r); rp.bgColor[1] = float(g); rp.bgColor[2] = float(b);

        std::unique_ptr<Image> bg;
        acut::RGBAImage B;
        if (rp.bgKind == acut::kBgClip && bg_ && bg_->isConnected()) {
            bg.reset(bg_->fetchImage(args.time));
            if (bg && bg->getPixelDepth() == eBitDepthFloat && bg->getPixelComponents() == ePixelComponentRGBA) {
                const OfxRectI bb = bg->getBounds();
                B.w = bb.x2 - bb.x1; B.h = bb.y2 - bb.y1; B.data = static_cast<float*>(bg->getPixelAddress(bb.x1, bb.y1));
                B.stride = ptrdiff_t(bg->getRowBytes()) / ptrdiff_t(sizeof(float));
                rp.bgImage = &B;
            } else rp.bgKind = acut::kBgSolid;
        } else if (rp.bgKind == acut::kBgClip) rp.bgKind = acut::kBgSolid;

        std::shared_ptr<acut::Matte> matte;
        std::string dir = matteDir();
        if (!dir.empty()) matte = gMatteCache.get(dir, frameIndex(args.time));

        try {
            if (S.w == D.w && S.h == D.h) {
                acut::render(S, matte.get(), rp, D);
            } else {
                // size mismatch (unexpected with tiles/multi-res disabled): copy the overlap untouched
                const int w = std::min(S.w, D.w), h = std::min(S.h, D.h);
                for (int y = 0; y < h; ++y) {
                    const float* s = S.px(0, y); float* d = D.px(0, y);
                    for (int x = 0; x < w * 4; ++x) d[x] = s[x];
                }
            }
        } catch (const std::exception& e) {
            acut::logf("render exception: %s", e.what());
            for (int y = 0; y < std::min(S.h, D.h); ++y) {
                const float* s = S.px(0, y); float* d = D.px(0, y);
                for (int x = 0; x < std::min(S.w, D.w) * 4; ++x) d[x] = s[x];
            }
        }
    }

private:
    Clip *dst_ = nullptr, *src_ = nullptr, *bg_ = nullptr;
    ChoiceParam *mode_, *tool_, *paint_, *quality_, *output_, *preview_, *bgKind_;
    StringParam *source_, *matteSet_, *clicks_, *exportDir_, *stState_, *stFrame_, *stTracking_, *stConf_, *stEta_, *stMsg_;
    IntParam *offset_, *revision_;
    DoubleParam *brush_, *brushFeather_, *refine_, *feather_, *smooth_, *shift_, *spill_, *decon_, *overlayOpacity_, *progress_;
    RGBParam* bgColor_;
    BooleanParam* premult_;
    std::shared_ptr<JobState> job_;
};

// ================================================================================================
// Viewer overlay: click selection, brush painting, status readout
// ================================================================================================
class AICutoutInteract : public OverlayInteract {
public:
    AICutoutInteract(OfxInteractHandle h, ImageEffect* effect)
        : OverlayInteract(h), plugin_(static_cast<AICutoutPlugin*>(effect)) {}

    static bool rodOf(AICutoutPlugin* p, double t, OfxRectD& rod) {
        try { rod = p->srcClip()->getRegionOfDefinition(t); } catch (...) { return false; }
        return rod.x2 > rod.x1 && rod.y2 > rod.y1;
    }

    bool draw(const DrawArgs& a) override {
        auto* suite = OFX::Private::gDrawSuite;
        OfxDrawContextHandle ctx = suite ? a.context : nullptr;
        if (!ctx) return false;
        OfxRectD rod;
        if (!rodOf(plugin_, a.time, rod)) return false;
        const double W = rod.x2 - rod.x1, H = rod.y2 - rod.y1;
        const int rel = plugin_->relFrame(a.time);
        const double px = a.pixelScale.x > 0 ? a.pixelScale.x : 1.0;

        // click points on this frame
        for (auto& c : plugin_->clicks()) {
            if (c.frame != rel) continue;
            double x = rod.x1 + c.u * W, y = rod.y2 - c.v * H;
            OfxRGBAColourF col = c.label ? OfxRGBAColourF{0.15f, 1.f, 0.25f, 1.f} : OfxRGBAColourF{1.f, 0.2f, 0.2f, 1.f};
            suite->setColour(ctx, &col);
            suite->setLineWidth(ctx, 2.f);
            double r = 7.0 * px;
            OfxPointD e[] = {{x - r, y - r}, {x + r, y + r}};
            suite->draw(ctx, kOfxDrawPrimitiveEllipse, e, 2);
            OfxPointD h1[] = {{x - r * 0.6, y}, {x + r * 0.6, y}};
            suite->draw(ctx, kOfxDrawPrimitiveLines, h1, 2);
            if (c.label) { OfxPointD v1[] = {{x, y - r * 0.6}, {x, y + r * 0.6}}; suite->draw(ctx, kOfxDrawPrimitiveLines, v1, 2); }
        }
        // brush strokes in progress
        if (!stroke_.empty()) {
            OfxRGBAColourF col = strokeAdd_ ? OfxRGBAColourF{0.2f, 0.9f, 1.f, 0.9f} : OfxRGBAColourF{1.f, 0.6f, 0.1f, 0.9f};
            suite->setColour(ctx, &col);
            suite->setLineWidth(ctx, float(std::max(1.0, plugin_->brushSize() * 2.0 / std::max(px, 1e-6) * 0.0 + 2.0)));
            if (stroke_.size() > 1) suite->draw(ctx, kOfxDrawPrimitiveLineStrip, stroke_.data(), int(stroke_.size()));
        }
        // brush cursor
        if (plugin_->paintMode() != kPaintOff && hover_) {
            OfxRGBAColourF col{1.f, 1.f, 1.f, 0.9f};
            suite->setColour(ctx, &col);
            suite->setLineWidth(ctx, 1.f);
            double r = plugin_->brushSize() * (W / 1920.0);
            OfxPointD e[] = {{pos_.x - r, pos_.y - r}, {pos_.x + r, pos_.y + r}};
            suite->draw(ctx, kOfxDrawPrimitiveEllipse, e, 2);
        }
        // status banner
        plugin_->refreshStatus();
        auto js = plugin_->job();
        std::string state, msg, conf, eta;
        double prog;
        {
            std::lock_guard<std::mutex> lk(js->m);
            state = js->state; msg = js->message; conf = js->conf; eta = js->eta; prog = js->progress;
        }
        std::string line = "AI Cutout: " + state;
        if (state == "Tracking" || state == "Analyzing" || state == "Opening" || state == "Rendering") {
            char b[128]; std::snprintf(b, sizeof(b), "  %.0f%%  conf %s  ETA %s", prog, conf.c_str(), eta.c_str()); line += b;
        }
        int tool = plugin_->toolMode();
        if (tool != kToolOff) line += tool == kToolAdd ? "   [click = include subject]" : "   [click = exclude]";
        if (plugin_->paintMode() != kPaintOff) line += plugin_->paintMode() == kPaintAdd ? "   [paint: add to mask]" : "   [paint: remove from mask]";
        OfxRGBAColourF tc{1.f, 1.f, 0.4f, 1.f};
        suite->setColour(ctx, &tc);
        OfxPointD tp[] = {{rod.x1 + 14 * px, rod.y2 - 26 * px}};
        suite->drawText(ctx, line.c_str(), tp, kOfxDrawTextAlignmentLeft);
        // "not analyzed" hint
        std::string dir = plugin_->matteDir();
        if (dir.empty() || acut::readMeta(dir).frames == 0) {
            OfxPointD tp2[] = {{rod.x1 + 14 * px, rod.y2 - 48 * px}};
            suite->drawText(ctx, "No matte yet: pick 'Add Selection', click the subject, then Analyze and Track.", tp2, kOfxDrawTextAlignmentLeft);
        }
        return true;
    }

    bool penMotion(const PenArgs& a) override {
        pos_ = a.penPosition; hover_ = true;
        if (down_) {
            if (stroke_.empty() || std::hypot(pos_.x - stroke_.back().x, pos_.y - stroke_.back().y) > 2.0 * std::max(a.pixelScale.x, 1e-6))
                stroke_.push_back(pos_);
            requestRedraw();
            return true;
        }
        if (plugin_->paintMode() != kPaintOff) { requestRedraw(); }
        return false;
    }

    bool penDown(const PenArgs& a) override {
        OfxRectD rod;
        if (!rodOf(plugin_, a.time, rod)) return false;
        pos_ = a.penPosition; hover_ = true;
        const int paint = plugin_->paintMode(), tool = plugin_->toolMode();
        if (paint != kPaintOff) {
            down_ = true; strokeAdd_ = paint == kPaintAdd; stroke_.clear(); stroke_.push_back(pos_);
            requestRedraw();
            return true;
        }
        if (tool == kToolOff) return false;
        const double u = (a.penPosition.x - rod.x1) / (rod.x2 - rod.x1);
        const double v = (rod.y2 - a.penPosition.y) / (rod.y2 - rod.y1);       // top-down
        if (u < 0 || u > 1 || v < 0 || v > 1) return false;
        const int rel = plugin_->relFrame(a.time);
        auto cl = plugin_->clicks();
        cl.push_back({rel, u, v, tool == kToolAdd ? 1 : 0});
        plugin_->beginEditBlock("AI Cutout: add selection point");
        plugin_->setClicks(cl);
        // immediate feedback: if a clip is open in the service, segment right away (fast, decoder only)
        std::string fields = "\"frame\":" + std::to_string(rel) + ",\"offset\":" + std::to_string(plugin_->offsetValue()) +
                             ",\"x\":" + std::to_string(u) + ",\"y\":" + std::to_string(v) + ",\"label\":" + std::to_string(tool == kToolAdd ? 1 : 0);
        acut::Reply r = acut::ServiceLink::call("add_point", fields, 3000);
        if (r.ok) plugin_->bumpRevision();
        plugin_->endEditBlock();
        requestRedraw();
        return true;
    }

    bool penUp(const PenArgs& a) override {
        if (!down_) return false;
        down_ = false;
        OfxRectD rod;
        if (stroke_.empty() || !rodOf(plugin_, a.time, rod)) { stroke_.clear(); return true; }
        // stroke -> proxy-independent normalized coordinates -> service
        std::string pts = "[";
        for (size_t i = 0; i < stroke_.size(); ++i) {
            double u = (stroke_[i].x - rod.x1) / (rod.x2 - rod.x1), v = (rod.y2 - stroke_[i].y) / (rod.y2 - rod.y1);
            char b[64]; std::snprintf(b, sizeof(b), "%s[%.5f,%.5f]", i ? "," : "", u, v); pts += b;
        }
        pts += "]";
        const int rel = plugin_->relFrame(a.time);
        double brushNorm = plugin_->brushSize() / 1920.0;             // brush radius as a fraction of frame width
        std::string fields = "\"frame\":" + std::to_string(rel) + ",\"offset\":" + std::to_string(plugin_->offsetValue()) +
                             ",\"add\":" + (strokeAdd_ ? "[" + pts + "]" : "[]") + ",\"remove\":" + (strokeAdd_ ? "[]" : "[" + pts + "]") +
                             ",\"brush_norm\":" + std::to_string(brushNorm);
        acut::Reply r = acut::ServiceLink::call("paint", fields, 5000);
        if (r.ok) plugin_->bumpRevision();
        else plugin_->sendMessage(Message::eMessageError, "", r.error);
        stroke_.clear();
        requestRedraw();
        return true;
    }

    void loseFocus(const FocusArgs&) override { hover_ = false; down_ = false; stroke_.clear(); }

private:
    AICutoutPlugin* plugin_;
    OfxPointD pos_{0, 0};
    bool hover_ = false, down_ = false, strokeAdd_ = true;
    std::vector<OfxPointD> stroke_;
};

class AICutoutOverlayDescriptor : public DefaultEffectOverlayDescriptor<AICutoutOverlayDescriptor, AICutoutInteract> {};

// ================================================================================================
// Factory
// ================================================================================================
class AICutoutFactory : public PluginFactoryHelper<AICutoutFactory> {
public:
    AICutoutFactory() : PluginFactoryHelper<AICutoutFactory>(kPluginIdentifier, kPluginVersionMajor, kPluginVersionMinor) {}
    void describe(ImageEffectDescriptor& d) override;
    void describeInContext(ImageEffectDescriptor& d, ContextEnum ctx) override;
    ImageEffect* createInstance(OfxImageEffectHandle h, ContextEnum) override { return new AICutoutPlugin(h); }
};

static std::string sChoiceHint;

static ChoiceParamDescriptor* defChoice(ImageEffectDescriptor& d, const char* name, const char* label, const char* hint,
                                        std::initializer_list<const char*> opts, int def, GroupParamDescriptor* g) {
    ChoiceParamDescriptor* p = d.defineChoiceParam(name);
    p->setLabel(label); p->setHint(hint);
    for (auto* o : opts) p->appendOption(o);
    p->setDefault(def);
    p->setAnimates(false);
    if (g) p->setParent(*g);
    return p;
}
static DoubleParamDescriptor* defDouble(ImageEffectDescriptor& d, const char* name, const char* label, const char* hint,
                                        double def, double lo, double hi, GroupParamDescriptor* g, double inc = 0.1) {
    DoubleParamDescriptor* p = d.defineDoubleParam(name);
    p->setLabel(label); p->setHint(hint); p->setDefault(def); p->setRange(lo, hi); p->setDisplayRange(lo, hi); p->setIncrement(inc);
    p->setDoubleType(eDoubleTypePlain);
    if (g) p->setParent(*g);
    return p;
}
static PushButtonParamDescriptor* defButton(ImageEffectDescriptor& d, const char* name, const char* label, const char* hint, GroupParamDescriptor* g) {
    PushButtonParamDescriptor* p = d.definePushButtonParam(name);
    p->setLabel(label); p->setHint(hint);
    if (g) p->setParent(*g);
    return p;
}
static StringParamDescriptor* defString(ImageEffectDescriptor& d, const char* name, const char* label, const char* hint,
                                        StringTypeEnum type, GroupParamDescriptor* g, bool readonly, bool hidden, bool persist) {
    StringParamDescriptor* p = d.defineStringParam(name);
    p->setLabel(label); p->setHint(hint); p->setStringType(type); p->setDefault("");
    p->setAnimates(false);
    if (readonly) p->setEnabled(false);
    if (hidden) p->setIsSecret(true);
    p->setIsPersistant(persist);
    p->setEvaluateOnChange(false);
    if (g) p->setParent(*g);
    return p;
}
static GroupParamDescriptor* defGroup(ImageEffectDescriptor& d, const char* name, const char* label, bool open = true) {
    GroupParamDescriptor* g = d.defineGroupParam(name);
    g->setLabel(label); g->setOpen(open);
    return g;
}

void AICutoutFactory::describe(ImageEffectDescriptor& d) {
    d.setLabels(kPluginName, kPluginName, kPluginName);
    d.setPluginGrouping(kPluginGrouping);
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
    // CPU render for now: Resolve hands us CPU buffers. GPU (CUDA/OpenCL) render kernels are future work.
    d.setSupportsCudaRender(false);
    d.setSupportsOpenCLRender(false);
    d.setOverlayInteractDescriptor(new AICutoutOverlayDescriptor());
}

void AICutoutFactory::describeInContext(ImageEffectDescriptor& d, ContextEnum ctx) {
    ClipDescriptor* src = d.defineClip(kOfxImageEffectSimpleSourceClipName);
    src->addSupportedComponent(ePixelComponentRGBA);
    src->setTemporalClipAccess(false);
    src->setSupportsTiles(false);
    src->setIsMask(false);
    ClipDescriptor* dst = d.defineClip(kOfxImageEffectOutputClipName);
    dst->addSupportedComponent(ePixelComponentRGBA);
    dst->setSupportsTiles(false);
    if (ctx == eContextGeneral) {
        ClipDescriptor* bg = d.defineClip("Background");
        bg->addSupportedComponent(ePixelComponentRGBA);
        bg->setOptional(true);
        bg->setSupportsTiles(false);
        bg->setTemporalClipAccess(false);
    }

    PageParamDescriptor* page = d.definePageParam("Controls");
    auto add = [&](ParamDescriptor* p) { page->addChild(*p); return p; };

    // ---- Source / link -------------------------------------------------------------------------
    GroupParamDescriptor* gSrc = defGroup(d, "grpSource", "Source");
    StringParamDescriptor* sf = defString(d, "sourceFile", "Source File",
        "The original media file of this clip. The AI service reads frames from it (Resolve does not expose the path to plugins).",
        eStringTypeFilePath, gSrc, false, false, true);
    add(sf);
    add(defString(d, "matteSet", "Matte Set", "Cache set used for rendering. Empty = the most recently analyzed clip.",
                  eStringTypeSingleLine, gSrc, false, false, true));
    DoubleParamDescriptor* dummy = nullptr; (void)dummy;
    IntParamDescriptor* off = d.defineIntParam("frameOffset");
    off->setLabel("Frame Offset"); off->setHint("Shifts which frame of the source file this clip frame maps to (for trimmed clips).");
    off->setDefault(0); off->setRange(-100000, 100000); off->setDisplayRange(-1000, 1000); off->setAnimates(false); off->setParent(*gSrc);
    add(off);
    add(defButton(d, "linkActive", "Link to Last Analyzed Clip", "Fills Matte Set and Source File from the most recent analysis.", gSrc));

    // ---- Mode ---------------------------------------------------------------------------------
    GroupParamDescriptor* gMode = defGroup(d, "grpMode", "Mode");
    add(defChoice(d, "mode", "Mode", "What to isolate. Person/Face auto-detect a starting region if you have not clicked.",
                  {"Person", "Object", "Face", "Custom"}, kModePerson, gMode));

    // ---- Selection ----------------------------------------------------------------------------
    GroupParamDescriptor* gSel = defGroup(d, "grpSelection", "Selection");
    add(defButton(d, "addSelection", "Add Selection", "Then click the subject in the viewer to include it.", gSel));
    add(defButton(d, "removeSelection", "Remove Selection", "Then click in the viewer to exclude a region.", gSel));
    add(defChoice(d, "clickTool", "Click Tool", "What a viewer click does.", {"Off", "Add Selection (+)", "Remove Selection (-)"}, kToolOff, gSel));
    add(defButton(d, "clearSelection", "Clear Selection", "Removes all selection points.", gSel));
    add(defDouble(d, "brushSize", "Brush Size", "Radius of the correction brush in pixels (at 1920 wide).", 24, 1, 300, gSel, 1));
    add(defDouble(d, "brushFeather", "Feather", "Softness of brush strokes (0 = hard).", 0.3, 0, 1, gSel, 0.05));
    add(defDouble(d, "edgeRefinement", "Edge Refinement", "How strongly the matte edge snaps to image edges.", 0.5, 0, 1, gSel, 0.05));
    add(defChoice(d, "paintMode", "Paint Mode", "Manual correction: paint in the viewer to add or remove mask.", {"Off", "Add Mask", "Remove Mask"}, kPaintOff, gSel));
    add(defButton(d, "correctFrame", "Correct Frame", "Make the current frame's mask a manual keyframe.", gSel));
    add(defButton(d, "propagateCorrection", "Propagate Correction", "Re-track from the corrected frame to the neighbouring keyframes.", gSel));
    (void)gMode;
    (void)gSrc;

    // ---- Tracking -----------------------------------------------------------------------------
    GroupParamDescriptor* gTrk = defGroup(d, "grpTracking", "Tracking");
    add(defButton(d, "trackForward", "Track Forward", "Track from the selected frame to the end of the clip.", gTrk));
    add(defButton(d, "trackBackward", "Track Backward", "Track from the selected frame to the start of the clip.", gTrk));
    add(defButton(d, "trackBoth", "Track Both Directions", "Track forward and backward from the selected frame.", gTrk));
    add(defButton(d, "recalculate", "Recalculate", "Redo tracking ignoring cached results (manual corrections are kept).", gTrk));

    // ---- Quality ------------------------------------------------------------------------------
    GroupParamDescriptor* gQ = defGroup(d, "grpQuality", "Quality");
    add(defChoice(d, "quality", "Quality", "Draft = fastest, High Quality = best edges (needs more GPU memory).", {"Draft", "Balanced", "High Quality"}, kQBalanced, gQ));

    // ---- Edge ---------------------------------------------------------------------------------
    GroupParamDescriptor* gEdge = defGroup(d, "grpEdge", "Edge");
    add(defDouble(d, "feather", "Feather", "Softens the matte edge (pixels).", 0, 0, 100, gEdge, 0.5));
    add(defDouble(d, "smooth", "Smooth", "Smooths jagged edges without softening (pixels).", 0, 0, 50, gEdge, 0.5));
    add(defDouble(d, "edgeShift", "Edge Shift", "Grow (+) or shrink (-) the matte (pixels).", 0, -100, 100, gEdge, 0.5));
    add(defDouble(d, "spillSuppression", "Spill Suppression", "Removes background colour spill from the subject.", 0, 0, 1, gEdge, 0.05));
    add(defDouble(d, "decontaminateEdge", "Decontaminate Edge", "Replaces edge colour with estimated foreground colour.", 0, 0, 1, gEdge, 0.05));

    // ---- Output -------------------------------------------------------------------------------
    GroupParamDescriptor* gOut = defGroup(d, "grpOutput", "Output");
    add(defChoice(d, "output", "Output", "Cutout writes a real alpha channel (use it to composite over another background).",
                  {"Mask", "Alpha", "Cutout", "Composite"}, kOutCutout, gOut));
    add(defChoice(d, "preview", "Preview", "Inspection view. Off = show the selected Output.",
                  {"Off", "Original", "Mask", "Alpha", "Transparent Checkerboard", "Overlay", "Cutout"}, kPrevOff, gOut));
    add(defChoice(d, "bgKind", "Composite Background", "Used when Output = Composite.",
                  {"Checkerboard", "Solid Color", "Background Clip"}, 0, gOut));
    RGBParamDescriptor* bc = d.defineRGBParam("bgColor");
    bc->setLabel("Background Color"); bc->setDefault(0.0, 0.6, 0.0); bc->setAnimates(true); bc->setParent(*gOut);
    add(bc);
    BooleanParamDescriptor* pm = d.defineBooleanParam("premultiply");
    pm->setLabel("Premultiply Output"); pm->setHint("Output premultiplied RGB (default: straight alpha)."); pm->setDefault(false); pm->setAnimates(false); pm->setParent(*gOut);
    add(pm);
    add(defDouble(d, "overlayOpacity", "Overlay Opacity", "Opacity of the Overlay preview.", 0.55, 0, 1, gOut, 0.05));
    add(defString(d, "exportFolder", "Render Folder", "Where Render writes the alpha/cutout PNG sequence. Empty = default.",
                  eStringTypeDirectoryPath, gOut, false, false, true));

    // ---- Buttons ------------------------------------------------------------------------------
    GroupParamDescriptor* gAct = defGroup(d, "grpActions", "Actions");
    add(defButton(d, "analyze", "Analyze", "Segment the selected subject on the selected frame(s).", gAct));
    add(defButton(d, "track", "Track", "Track the subject through the whole clip.", gAct));
    add(defButton(d, "previewButton", "Preview", "Toggle the Overlay preview.", gAct));
    add(defButton(d, "render", "Render", "Write the final alpha and cutout PNG sequence to the Render Folder.", gAct));
    add(defButton(d, "reset", "Reset", "Clear selection points and cached mattes for this clip.", gAct));
    add(defButton(d, "cancelJob", "Cancel", "Stop the running analysis/tracking job.", gAct));

    // ---- Status (read-only) ------------------------------------------------------------------
    GroupParamDescriptor* gSt = defGroup(d, "grpStatus", "Status");
    add(defString(d, "stState", "State", "", eStringTypeSingleLine, gSt, true, false, false));
    add(defString(d, "stFrame", "Current Frame", "", eStringTypeSingleLine, gSt, true, false, false));
    add(defString(d, "stTracking", "Tracking Status", "", eStringTypeSingleLine, gSt, true, false, false));
    add(defString(d, "stConfidence", "AI Confidence", "", eStringTypeSingleLine, gSt, true, false, false));
    add(defString(d, "stEta", "Estimated Time Remaining", "", eStringTypeSingleLine, gSt, true, false, false));
    DoubleParamDescriptor* pr = defDouble(d, "progress", "Progress %", "", 0, 0, 100, gSt, 1);
    pr->setEnabled(false); pr->setIsPersistant(false); pr->setEvaluateOnChange(false);
    add(pr);
    add(defString(d, "stMessage", "Message", "", eStringTypeMultiLine, gSt, true, false, false));

    // ---- hidden persistent state ---------------------------------------------------------------
    add(defString(d, "clicks", "Clicks", "Selection points (frame,u,v,label;...)", eStringTypeSingleLine, nullptr, false, true, true));
    IntParamDescriptor* rev = d.defineIntParam("revision");
    rev->setLabel("Revision"); rev->setDefault(0); rev->setIsSecret(true); rev->setAnimates(false); rev->setEvaluateOnChange(true);
    add(rev);
}

void OFX::Plugin::getPluginIDs(OFX::PluginFactoryArray& ids) {
    static AICutoutFactory factory;
    ids.push_back(&factory);
}
