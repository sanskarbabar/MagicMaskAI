# AI Cutout — Architecture

> Written in Phase 0 (research) and updated to the **as-built v0.1** state. Section 0.1 is the authoritative summary of
> what exists; where the older text below disagrees with 0.1, 0.1 wins (each such place is marked *As built*).

> AI-powered video subject isolation and tracking for DaVinci Resolve.
> This is **not** a replacement for, or a clone of, Blackmagic Design's Magic Mask, and does not
> integrate with it. Capabilities are tagged by how they are known:
>
> - **[VERIFIED-SDK]** confirmed by reading the Resolve 21.1 Developer SDK installed on this machine
>   (`C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer`, OpenFX README dated 12 May 2026).
> - **[VERIFIED-WEB]** confirmed from a web source during this research (listed in `THIRD_PARTY_LICENSES.md`).
> - **[UNVERIFIED]** believed true but must be proven by a probe inside a real Resolve before we depend on it
>   (see `docs/PHASE1_PROBES.md`).
> - **[DECISION]** an engineering choice, not a fact.

## 0.1 As built (v0.2) — read this first

v0.2 simplified the product to the KVN-Rotoscope style of workflow: **one app does everything, the Resolve effect only shows the result.**
Sections below that describe the viewer overlay, in-plugin Analyze/Track buttons, paint modes, Studio panels or 57 parameters are the
*original Phase-0 plan*; they were deliberately dropped.

```
 AI Cutout app (Tk) ──loopback JSON──> local service (Python) ──> SAM 2 (ONNX Runtime, DirectML/CUDA) + optical flow
        │  open · click · Track · Render                             writes per-frame mattes to the cache
        ▼
   %LOCALAPPDATA%\AICutout\cache\<clip set>\masks\*.acm   ◄── read by ──  AICutout.ofx (Resolve effect, CPU, C++)
```

| Part | State |
|---|---|
| **Resolve effect** `AICutout.ofx`: Open AI Cutout, Update Matte, Output (Cutout/Matte/Overlay/Checkerboard/Original), Feather, Edge Shift, Clean Edge Colors, Frame Offset | Built; loads in Resolve Free 21.1.0.17; contract/render tested against a mock OpenFX host. No overlay, threads, sockets or ML inside it (imports only Windows system DLLs). Not tested on the Resolve timeline. |
| **App**: Open → click subject → Track → Render; click any frame to fix it; timeline of tracked / low-confidence frames; progress + ETA | Built; driven end to end by an automated test. |
| **Service**: SAM 2 segmentation, flow-propagate + SAM refine tracking with keyframes, appearance gate, re-acquisition, temporal stabilizer, CRC'd matte cache, full-resolution export | Built + tested + benchmarked (docs/BENCHMARKS.md). |
| **Edge pipeline** (C++, multi-threaded CPU): cleanup, edge shift, guided-filter refinement, feather, decontamination/spill, outputs | Built + tested; same code in the effect and the app's Render. Fixed defaults (refine 0.5, balanced quality); no GPU kernels. |
| **Installer** (Inno Setup; PyInstaller-frozen app+service) | Built; install/uninstall logic tested via the per-user variant. The admin installer is compiled the same way but not run here. |
| Dropped from the original plan | viewer overlay & in-Resolve clicking, in-plugin analysis jobs, brush painting, Mode/Quality/Edge parameter groups, Composite-over-background, Workflow-Integration panel, CUDA/OpenCL render kernels, SAM 2 memory tracker, hair-matting model, macOS |

IPC is **loopback TCP on 127.0.0.1 plus a per-session token** (stored in `%LOCALAPPDATA%\AICutout\service.json`), used only between the app and the service;
the effect does not talk to the service at all — it just reads the cache. The service commands are: `hello, open, status, timeline, get_frame, get_mask, segment, commit,
refine, track, cancel, reset, export, shutdown`.

## 0. Target edition: DaVinci Resolve **Free** (confirmed by the user, 21.1)

Consequences that override anything later in this document:
- **Workflow Integration panels are out** (documented Studio-only). The **Companion window** (own process, own UI) is
  therefore the primary place for progress, timeline, confidence and frame-based point picking — not an optional extra.
- **External scripting is out** (documented under Studio). Only *internal* scripts run from `Workspace > Scripts`
  are candidates, and whether Free permits them, and which API calls work, is **[UNVERIFIED]** (probe P7). The design
  must work with **zero** Resolve scripting: clip identity/source path may have to come from a user-triggered internal
  script, from a file the user picks in the Companion, or from OFX frame fetching.
- **Whether Free loads third-party OFX** was the go/no-go gate. *As built:* **PASSED** — Resolve 21.1.0.17 Free logged
  `OFX: loading com.aicutout.AICutout` for our bundle (and does so for Topaz). What still needs a person in Resolve is the
  interactive behaviour (probes P1b–P10).
- Free-specific limits to expect and measure: GPU handling (single GPU), resolution/output caps (UHD max on Free), no
  Studio-only AI extras. None of these are Studio-dependent for our own service, which runs outside Resolve.

## 1. Reference machine

| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200 |
| Resolve | 21.1.0.17 **Free** |
| GPU | NVIDIA GeForce RTX 4060 Laptop, 8 GB VRAM, driver 560.94; Intel UHD (iGPU) |
| Existing 3rd-party OFX | Topaz Video AI (`C:\Program Files\Common Files\OFX\Plugins`) — proves third-party OFX loads on this install |
| Present | Python 3.12, Git, Node |
| Toolchain used | llvm-mingw 22 (user-scope install; MSVC Build Tools need admin), CMake 4.4, Inno Setup 6, ONNX Runtime-DirectML |

## 2. What Resolve actually offers third parties

### 2.1 OpenFX (OFX 1.4 headers ship with Resolve) — the render path
**[VERIFIED-SDK]**
- CPU processing plus **GPU render via CUDA (with host-provided `cudaStream`), OpenCL, and Metal**
  (`ofxGPURender.h`; Gain sample sets `setSupportsCudaRender/CudaStream/OpenCLRender`). Buffers arrive as
  device pointers when `kOfxImageEffectPropCudaEnabled=1`.
- **Temporal / random frame access** inside `render` via `clip->fetchImage(t)` after declaring
  `setTemporalClipAccess(true)` + `getFramesNeeded` (samples: `TemporalBlurPlugin`, `RandomFrameAccessPlugin`).
- **Overlay interacts** (on-viewer drawing and input): `OFX::OverlayInteract` with the **Draw Suite**
  (`ofxDrawSuite.h`: lines, line strips/loops, filled rectangle, filled polygon, unfilled ellipse, text) and
  input actions `PenDown / PenMotion / PenUp` (position, pressure), `KeyDown/KeyUp`. The Resolve-shipped
  `GainPlugin` uses an overlay interact, so the API is intended to work in Resolve.
- Parameters: double, int, bool, choice, string, custom, button (push), group, page, RGBA, 2D/3D points.
- Filter context (1 input) and general context (extra clips); output RGBA float with alpha.
- Packaging: `Name.ofx.bundle/Contents/Win64/Name.ofx` in `C:\Program Files\Common Files\OFX\Plugins`.
  On Windows-on-ARM Resolve scans `Win-arm64x`, `Win-arm64ec`, `Win64`.

**What OFX cannot do (by design)**
- No custom widgets: no progress bar, thumbnails, timeline strip, confidence graph, or free-form panels. UI is the
  host-generated parameter list only. Progress/status must be *text* (a string/label param, overlay text) or live
  outside Resolve.
- The Draw Suite draws **vector primitives only** (no bitmaps). A mask preview on the viewer therefore has to be
  produced by the **render** (Overlay / Mask output modes), not by the overlay.
- A plugin is invoked by the host; it does not get to "walk the whole clip" on demand. Long-running analysis is
  not a native OFX concept, and OFX has no reliable way to know the source media file path. **[UNVERIFIED]** that
  `fetchImage` is legal from a button-press `changedParam` on a worker thread in Resolve (probe P3).
- Only the host decides how results are cached; Resolve invalidates its render cache when *parameters* change.
  We therefore need a param-bump mechanism when new mattes land on disk (probe P8).
- Multiple-input effects (Composite-over-background) may only expose extra inputs on some pages (Fusion) (probe P4).

### 2.2 Resolve scripting API (Python 3.14 embedded from 21.1; Lua) — the orchestration path
**[VERIFIED-SDK]**
- Scripts run from `Workspace > Scripts` (folders under `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts`
  or `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts`), or externally when
  *Preferences > System > General > External scripting* is enabled. The README documents the external setting
  under "DaVinci Resolve **Studio**" (edition limits of internal scripting in Free: **[UNVERIFIED]**, probe P7).
- It can read project/timeline/clip structure, markers, media-pool items and clip properties, and drive renders.
  The README contains **no OFX-parameter access**, no viewer hooks, and no Magic Mask API. So scripting *complements*
  OFX (media metadata, render jobs, markers) but cannot replace it and cannot read/write our plugin's params.

### 2.3 Workflow Integration plugins (Electron + `WorkflowIntegration.node`)
**[VERIFIED-SDK]** Studio only, Windows/macOS. Installed to
`%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\`, listed under
*Workspace > Workflow Integrations*. This is the only in-Resolve place for a real custom panel (progress bar,
timeline strip, confidence graph). Because it is Studio-only, **it is an optional enhancement, not a dependency**.

### 2.4 Others
- **Fusion Fuse / DCTL**: real-time GPU pixel code, but no Python/ML, no process spawning, no file IO — only useful for
  small kernels. Not used.
- **Fusion scripting** (`Comp:` API): can create tools/comps in the Fusion page; may help place our OFX node and read
  frame ranges. Adds no ML or UI capability over OFX.
- **Codec plugins / OGraf**: irrelevant.

### 2.5 Answers to the "First Task" questions

| # | Question | Answer |
|---|---|---|
| 1 | Latest plugin APIs | OFX 1.4 + GPU render suite, Draw/Interact suites; Scripting (Py 3.14, 21.1); Workflow Integrations. All read from the local 21.1 SDK. |
| 2 | OFX can / cannot | §2.1 |
| 3 | Scripting complement | Clip metadata, file path/in-out, markers, render jobs, node placement — §2.2 |
| 4 | Interactive viewer selection possible? | **Yes in principle**: overlay interact with pen events + Draw Suite is an official API and shipped in Resolve's own sample. Whether it fires on the Edit-page and Color-page viewers (vs only Fusion) is **[UNVERIFIED]** → probe P2. Click points are stored in hidden string params so they persist in the project. |
| 5 | Best AI architecture | Out-of-process local service (§3, §4). |
| 6 | Models + licensing | §5 and `THIRD_PARTY_LICENSES.md` |
| 7 | Windows install locations | §7 |
| 8 | GPU options | §6 |
| 9 | Architecture doc | this file |
| 10 | Limitations | §9 |

## 3. Decision: where does inference run?

**[DECISION] Out-of-process local service. The OFX plugin contains no ML runtime.**

Reasons:
1. In-process ML inside Resolve means loading ONNX Runtime/CUDA/cuDNN DLLs into Resolve's address space, beside
   Resolve's own copies. The installed Topaz plugin ships `onnxruntime-topaz.dll`, `DirectML-topaz.dll`,
   `nvinfer_10-topaz.dll`, `opencv_world481.dll` with vendor-suffixed names — evidence that DLL collisions are a real
   problem in this ecosystem. A tiny plugin DLL avoids it entirely.
2. A crash or OOM in a model must not take down Resolve or the user's unsaved project.
3. Analysis is minutes-long batch work; OFX `render` must return in milliseconds. They are different workloads.
4. Model swap = swap service files, no OFX rebuild.

### Guiding principle: **render never waits for AI**
`render()` only reads already-computed mattes from the cache and does GPU edge/composite work. If a frame has no
matte, it passes the source through and shows "Not analyzed" — it never blocks playback on inference.
Analysis (SAM-family segmentation + tracking) happens ahead of time in the service; playback/render is cache-backed.
This is how 4K "fast cached playback after analysis" becomes realistic, and it means we do **not** promise real-time
4K AI.

## 4. Component architecture

```
┌──────────────────────── DaVinci Resolve ─────────────────────────┐
│  OFX host                       Scripting / Workflow Integration │
│    │  params, overlay, GPU frame        │ (optional, Studio-only  │
│    ▼                                    │  for external/panel)    │
│  AICutout.ofx  (C++, thin)  ◄──────────┘                          │
│   • params/UI • overlay clicks & brush • cache reader             │
│   • CUDA/OpenCL/CPU edge+composite kernels                        │
└────────┬──────────────────────────────────────────────────────────┘
         │ named pipe (user-only ACL) — control messages only (JSON)
         │ bulk data = memory-mapped files in the cache dir
┌────────▼──────────────────────────────────────────────────────────┐
│  aicutout-service  (Python, local only, no network sockets)       │
│   Session/Job manager → Frame source → SegmentationEngine         │
│                                         → Tracker → Temporal      │
│                                         → Edge/matting → Cache    │
│   GPU backends: CUDA/TensorRT EP · DirectML EP · CPU              │
└───────────────────────────────────────────────────────────────────┘
        optional: AICutout Companion (own window: progress, timeline, confidence) — works in Free
        optional: Workflow Integration panel (Studio only)
```

Layers, matching the requested separation:
`UI (OFX params · overlay · companion) → Plugin API (ipc protocol) → Core engine (Python: session, cache, temporal,
edge) → AI engine (SegmentationEngine implementations) → GPU backend (ORT providers / CUDA kernels)`.

### 4.1 Repository layout
```
plugin/OFX/        C++ plugin: describe, params, overlay, render, kernels/{cuda,opencl,cpu}
plugin/Resolve/    scripting helpers (clip path, in/out, retime, markers) behind a ResolveBridge interface
plugin/UI/         Companion app (progress/timeline/confidence)
core/segmentation/ SegmentationEngine ABC + registry
core/tracking/     propagation, keyframe segments, confidence
core/temporal/     flow-guided smoothing, jump limiter
core/masking/      morphology, guided filter, matting hand-off
core/compositing/  reference (CPU) implementations used by tests; GPU versions live in plugin kernels
core/cache/        hashing, index, memory-mapped matte store
inference/         service entry point, IPC server, job scheduler
gpu/               device detection, provider selection, VRAM budgeting
models/            manifests + download/verify (no weights committed)
installer/         Inno Setup script
tests/  docs/
```
The Resolve version lives in exactly one place (`plugin/Resolve/compat.py` + a documented version matrix);
OFX 1.4 itself is stable across Resolve versions.

### 4.2 SegmentationEngine abstraction
```
class SegmentationEngine(ABC):
    info() -> ModelInfo{id, version, license, vram_estimate, supports{points,box,video,text}}
    load(device, precision) ; unload()
    set_image / encode_frame(frame) -> FrameEmbedding
    segment(embedding, prompts) -> MaskProposal{mask, confidence}
class VideoSegmentationEngine(SegmentationEngine):
    init_track(frame_idx, prompts) -> TrackState
    propagate(state, frames, direction) -> iterator[FrameResult{mask, confidence, object_present}]
    add_correction(state, frame_idx, mask|prompts)
```
Registered by manifest (`models/<id>/manifest.json`: file hashes, license id, min VRAM). Mode mapping is policy, not code:
Person/Object/Face/Custom choose prompt presets and optional post-steps (e.g. Face = face-region prior),
they do not hardwire a model. Engines are ordered by capability; missing model ⇒ next engine + a visible message.

### 4.3 Tracking pipeline

> *As built:* SAM 2's **video memory** graphs are not exported to ONNX by the model source we use (only the image encoder/decoder are), so the tracker is
> **flow-propagate + SAM 2 refine**: previous mask → robust optical-flow warp → prompts (box + interior points + mask) → SAM 2 decoder, with a
> clean-reference chain, appearance gate, area-drift guard, constant-velocity prior during occlusion and colour-model re-acquisition. It is
> measured in `docs/BENCHMARKS.md`; it is *not* SAM 2's own memory tracker.
1. Decode analysis-resolution frames (GPU decode where available).
2. User positive/negative points (and optional box) → **initial mask** on the reference frame; user confirms.
3. **Propagate** with a memory-based video segmenter (SAM 2-family): each frame is conditioned on a memory bank of
   past frames + reference frames; the model outputs an *object-present* score (handles occlusion/leave/re-enter).
   This is deliberately **not** per-frame independent segmentation.
4. **Reliability signals** per frame: object-present score, mask IoU prediction, area change rate, optical-flow
   consistency (warp previous mask, compare). Below threshold ⇒ mark frame `low_confidence`, stop or continue per
   policy, surface it in status, and let the user correct.
5. **Keyframe segments**: every AI reference / manual correction is a keyframe. Between two keyframes propagate forward
   from the left and backward from the right and blend by distance and confidence; beyond the last keyframe propagate
   forward. "Correct frame 120 → recalculate 121→180" = new keyframe at 120, re-run only the affected segment.
6. Mask refinement + temporal stabilization + edge pipeline (§4.4), then write to cache.

Camera motion/zoom/rotation/scale/lighting/background changes are handled by the segmenter's appearance memory plus
flow-guided warping; **measured** robustness per condition will be reported from the test suite, not assumed.

### 4.4 Temporal stability and edge pipeline
```
Raw mask → noise/island removal → morphology → source-guided edge refine (guided filter / optional matting on the
uncertain band) → temporal stabilization → feather → decontamination/spill → final alpha
```
- Temporal: flow-warped previous alpha blended with the new one using a **per-pixel weight driven by flow magnitude and
  confidence** (strong smoothing where the image is static, almost none where motion is fast → avoids lag); a jump
  limiter rejects sudden area expansion/shrink (>X% between frames unless confidence says otherwise); islands tracked
  over time so limbs/hair clumps don't blink out. Thresholds are tunable params validated by the stability metric in §8.
- Analysis-resolution mask is stored; **upscale + edge refine + feather + decontaminate run at full resolution in the
  OFX GPU kernels** using the original frame as guide. Sliders (Feather, Smooth, Edge Shift, Spill, Decontaminate) are
  therefore live, cache-independent, and cheap.
- Hair honesty: guided-filter refinement helps but is not true matting. High Quality may add a matting model on the
  unknown band (candidate: ViTMatte, license to be verified). Expect imperfect fine hair on hard backgrounds.
- Decontamination uses an approximate foreground-colour estimation (published algorithm, implemented by us).

### 4.5 Output modes
Original · Mask (gray) · Alpha · Checkerboard · Overlay · Cutout (RGB = foreground, **A = matte**) · Composite
(over a solid/checker, or a Background input clip where the page exposes one **[UNVERIFIED]**).
Cutout writes a genuine alpha channel on the output clip; whether the alpha reaches a downstream compositor depends on the
page (Edit-track compositing / Fusion: expected; Color page needs the node's alpha output routed) — probe P4.

### 4.6 Cache
Resolve projects live in a database, not a folder, so the requested `Project/AI_Cutout_Cache` is adapted:
```
%LOCALAPPDATA%\AICutout\cache\            (root configurable)
 └─ <clip_key>\<settings_hash>-<model_id>@<model_version>\
      ├─ masks.bin      chunked, memory-mapped 8-bit mattes @ analysis res, per-frame index + CRC
      ├─ tracking.json  keyframes, per-frame confidence, flags
      ├─ metadata.json  source info, engine, schema version
      └─ previews/      small thumbnails for the companion/timeline
```
- `clip_key` = hash of (source path + size + mtime + frame count + fps + in/out + retime) from the scripting helper;
  fallback when no helper: hash of dimensions/fps/frame-count + downscaled content of sampled frames.
- Key includes model id+version and the analysis-affecting settings (quality tier, mode, prompts). Edge/feather
  sliders are **not** in the key (they're applied at render time).
- Each frame record has a CRC; a corrupt record is dropped and recomputed. Analysis skips frames already cached.
- User state that must survive project save/copy (click points, brush strokes as RLE, keyframe list, cache key) is
  stored in hidden OFX string params **[UNVERIFIED]** (probe P8).

### 4.7 IPC
*As built:* **loopback TCP on 127.0.0.1 (ephemeral port) plus a random per-session token** stored in
`%LOCALAPPDATA%\AICutout\service.json`; every request must carry the token. The listener is bound to the loopback
interface only, so it is not reachable from the network and no video leaves the machine. (The original plan was a
user-ACL'd named pipe; a loopback socket is identical on macOS and needs no Win32 pipe code in Python. Other processes of
the same Windows user can read the token file, as with any per-user secret.) One JSON object per line. Commands:
`hello, hardware, open, close, status, timeline, get_frame, get_mask, segment, commit, refine, correct, paint, add_point,
confirm, clear_points, track, analyze_track, cancel, reset, clear_range, list_sets, export, shutdown`. The service is
started on demand by the plugin/Companion (no admin, no Windows service, launcher path from `install.ini`) and exits after
an idle timeout (default 30 min).

## 5. Model selection (evidence + status)

Selection criteria: quality, temporal consistency, speed, VRAM, Windows support, license, redistribution.
Licenses below are what I confirmed during this research; anything not confirmed is marked and **must** be checked
against the actual LICENSE files/weights pages before it enters a release (see `THIRD_PARTY_LICENSES.md`).

| Candidate | Role | License (status) | Verdict |
|---|---|---|---|
| **SAM 2 / 2.1** (Meta) | promptable image + video segmentation with streaming memory | Apache-2.0 code + checkpoints (web, VERIFIED-WEB) | **Primary.** Video memory gives temporal consistency and occlusion handling. Sizes Tiny→Large let Draft/Balanced/High map to model size. |
| **EdgeTAM** (Meta) | SAM 2-class tracker, much faster | Apache-2.0 (web) | **Draft-tier candidate**; needs a Windows ONNX/ORT benchmark. Paper speed numbers are for iPhone, not our hardware. |
| **MobileSAM** | fast *image* segmenter for click preview | MIT vs Apache-2.0 conflicting in sources → read repo LICENSE | Interactive click preview candidate (image-only, no tracking). |
| **SAM 3 / 3.1** | text/concept prompts, video tracking | **Custom "SAM License"** (verified): commercial OK, redistribution allowed *with the license copy*, bans military/nuclear/weapons/ITAR uses, no reverse engineering | **Optional later engine only**, after legal review — not OSI-open, adds downstream use restrictions to our users. |
| **Cutie** | video object segmentation w/ memory | MIT (web) | Alternative tracker; no ONNX export confirmed. |
| **MODNet** | portrait matting | Apache-2.0 (web) | Person-only fallback / matting refinement candidate. |
| **BiRefNet** | high-res background removal | MIT code (web); some derived weights (e.g. RMBG-2.0) **non-commercial** | Candidate for High-Quality refine; use only the MIT-licensed weights, verify each file. |
| **RVM** (Robust Video Matting) | video matting | **GPL-3.0** (web) | **Excluded** — copyleft is incompatible with a redistributable closed plugin. |
| **MatAnyone** | video matting | **NTU S-Lab License** (non-commercial) (web) | **Excluded.** |
| **FastSAM** | fast segmenter | believed AGPL-3.0 (**not verified here**) | **Excluded until verified.** |

**[DECISION] Default engine: SAM 2.1** (Tiny/Small for Draft/Balanced, Base+/Large for High Quality if the VRAM
budget allows), behind `SegmentationEngine` so EdgeTAM/others slot in. On this 8 GB GPU shared with Resolve, VRAM is the
binding constraint; concrete model-size/VRAM/FPS numbers will come from Phase 3 benchmarks — none are asserted here.
Feasibility risk: SAM 2 *video* graphs must be exported to ONNX (image encoder, memory encoder, memory attention, mask
decoder). Community exports exist for the image path **[UNVERIFIED for the video path]**. Fallback: run PyTorch (CUDA)
in the service — bigger install, same architecture.

## 6. GPU strategy
> *As built:* **inference is GPU-accelerated** (ONNX Runtime DirectML provider on NVIDIA/AMD/Intel; a CUDA provider is used automatically
> if `onnxruntime-gpu` is installed); **OFX render is multi-threaded CPU C++** (the plugin does not declare CUDA/OpenCL render support, so
> Resolve hands it CPU frames). GPU render kernels remain the biggest missing performance feature; measured CPU render cost is in
> `docs/BENCHMARKS.md`. The remainder of this section is the target design.

- **Render side (OFX):** CUDA kernels on the host's stream (`CudaStreamSupported`) for NVIDIA; OpenCL kernels for
  AMD/Intel; CPU fallback via `multiThreadProcessImages`; Metal later on macOS. The source frame stays on the GPU.
  Per render only the small analysis-res matte is uploaded (~sub-MB) — this is the *only* host→device copy, avoiding
  GPU→CPU→GPU round trips of full frames.
- **Inference side (service):** ONNX Runtime providers chosen at startup: TensorRT/CUDA (NVIDIA), DirectML (AMD/Intel on
  Windows), CPU. Frames are decoded (NVDEC where available) straight into GPU tensors.
- **Deliberately not attempted:** sharing Resolve's in-flight GPU frame with the service process (would need CUDA/D3D
  interop across processes and is fragile); analysis reads source media, render consumes cached mattes instead.
- VRAM: query free memory before loading; refuse/redirect with the requested message
  ("GPU memory is insufficient for High Quality mode. Try Balanced or Draft.").

## 7. Windows install locations
| Component | Path |
|---|---|
| OFX bundle | `C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle\Contents\Win64\AICutout.ofx` **[VERIFIED-SDK]** |
| Service + runtime | `C:\Program Files\AICutout\` |
| Models | `%PROGRAMDATA%\AICutout\models\` |
| Cache (default) | `%LOCALAPPDATA%\AICutout\cache\` |
| Helper scripts (menu) | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\AICutout\` **[VERIFIED-SDK]** |
| Workflow Integration panel (Studio) | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\AICutout\` **[VERIFIED-SDK]** |

Installer: **Inno Setup** (free, scriptable, uninstaller, Resolve-path detection). Touches only the paths above; never
edits Resolve's own files. Production distribution needs an Authenticode signing certificate (else SmartScreen
warnings). Model weights are downloaded/verified by hash at install, or bundled if the license permits redistribution.

## 8. Testing & measurement plan (summary)
Synthetic + public-clip suites for: person/object/multi/small/large; static/moving camera, zoom, rotation, fast motion,
motion blur, occlusion, low light, high contrast; hair, transparency, reflections, shadows, similar-color background,
multiple people. Metrics: J&F/IoU vs ground truth (segmentation), temporal stability (frame-to-frame alpha
flicker/flow-warp error), FPS, latency, VRAM, CPU. Numbers get published in `docs/BENCHMARKS.md` from real runs on named
hardware; no performance claim ships without one.
In-Resolve testing after each phase is manual (Resolve has no headless plugin-test harness); `docs/PHASE1_PROBES.md` is the
checklist, and I cannot drive Resolve's GUI from here, so results must be reported back.

## 9. Limitations (updated to the as-built state)
> Items 4, 8 and 10 were open questions in Phase 0: **4 resolved** (Free loads our plugin), **8** (image path exported to ONNX; video memory graphs not used), **10** resolved (toolchain installed, llvm-mingw).
1. **No native Magic Mask hooks.** We can't read Resolve's masks, add nodes to its tracker, or put buttons in its UI beyond OFX params.
2. **Rich UI (progress bar, timeline, confidence graph) isn't possible in OFX.** Solution: text status in params +
   Companion window (all editions) + optional Workflow Integration panel (Studio only).
3. **Analysis is separate from Resolve's render engine**, not shown in Resolve's own progress UI, and needs the source
   frames via file path (scripting helper) or via OFX frame fetch (unproven). Clips with retiming/compound/Fusion
   sources, or codecs FFmpeg can't decode (e.g. BRAW/RAW variants), need the OFX fetch or a Resolve-rendered
   intermediate. This is the **largest technical risk** → probes P3/P7 first.
4. **Target is Free, and Free's third-party OFX support is unproven.** Search results conflict (one page claims Free
   doesn't load OFX; others list plugins working in Free). External scripting and Workflow Integrations are documented
   as Studio-only, so they are unavailable. Decided empirically by P1 (go/no-go) and P7.
5. **Viewer-click support on Edit/Color pages is unverified** (P2); Fusion-page viewer is the fallback.
6. **Hair, glass, reflections, shadows, thin motion-blurred limbs** are hard for any current model; expect manual
   correction. Marketing copy must not promise otherwise.
7. **VRAM contention** with Resolve on 8 GB cards; High Quality may not fit alongside a busy Resolve session.
8. **ONNX export of SAM 2 video graphs** is unproven; PyTorch fallback increases install size.
9. **Licensing:** SAM 3 is not OSI-open; RVM/MatAnyone/FastSAM excluded; every shipped weight file needs a recorded license.
10. **Build toolchain not yet installed** on this machine (MSVC, CMake, CUDA Toolkit, Inno Setup).
11. **Code signing** required for a clean Windows install experience in production.
12. macOS: Metal kernels, CoreML/ORT, Unix sockets, notarization — designed-for, not built.

## 10. Phase plan and result
*Result:* Phases 1–10 were implemented in one pass at the requester's instruction rather than gated one by one; the gate column is what still has to be confirmed **inside Resolve** by a person (see `docs/RESOLVE_TESTING.md`). Phase 8 (GPU render kernels) is **not** done; everything else exists and is covered by automated tests.

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | This doc, license register, probe checklist | reviewed |
| 1 | OFX plugin loads, appears, passes frame through; **run probes P1–P8** | probe results recorded; architecture adjusted to reality |
| 2 | CPU/GPU mask kernel + output modes (procedural mask) | alpha reaches a downstream comp |
| 3 | Service + SAM 2 single-frame segmentation; benchmarks; model choice frozen | measured VRAM/latency |
| 4 | Overlay click selection, pos/neg points, brush | clicks persist in project |
| 5 | Tracking, keyframes, low-confidence handling | test-suite J&F targets |
| 6 | Temporal stabilization | flicker metric improves without lag |
| 7 | Edge pipeline + decontamination | visual + metric checks |
| 8 | GPU (CUDA/OpenCL) render kernels, VRAM budgeting | frame-time budget met |
| 9 | Persistent cache + invalidation | hash/CRC tests |
| 10 | Installer/uninstaller, docs, signing | clean-VM install test |
