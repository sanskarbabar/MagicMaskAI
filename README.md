# AI Cutout

**AI-powered video subject isolation and tracking for DaVinci Resolve.**
Click a person or object, let a local AI segment it, track it through the clip, correct mistakes by hand, and output a
transparent cutout (a real alpha channel) to composite over another background. Everything runs on your computer — no video is ever uploaded.

> This is an independent third-party tool. It is not Magic Mask, does not use or replace it, and makes no compatibility claim with it.
> Features below are described as **implemented**, **tested** (automated tests in this repo) or **unverified** (needs a person in Resolve).

## What you get
| Piece | What it is |
|---|---|
| **AI Cutout** OpenFX plugin | Appears under *Effects → OpenFX → AI Cutout*. Mode (Person/Object/Face/Custom), Selection (click include/exclude, brush, feather, edge refinement), Tracking (forward/backward/both/recalculate), Quality (Draft/Balanced/High), Edge (feather, smooth, edge shift, spill suppression, decontaminate), Output (Mask/Alpha/Cutout/Composite), Preview (Original/Mask/Alpha/Checkerboard/Overlay/Cutout), buttons *Analyze / Track / Preview / Render / Reset*, and live status (state, frame, tracking, AI confidence, ETA). |
| **AI service** | Local process (SAM 2 on your GPU via ONNX Runtime/DirectML, CPU fallback). Segmentation, optical-flow tracking, temporal stabilization, matte cache. Loopback-only. |
| **Companion** | Desktop window with a frame viewer, timeline (tracked / low-confidence / keyframes / confidence curve), progress bar with ETA, paint corrections, preview modes and Render. Works in **every** Resolve edition. |
| **Installer** | Windows installer (admin) that places the plugin in the shared OpenFX folder, installs the service, Companion and models, and can be uninstalled cleanly. |

## Status (v0.1)
* Target: **Windows 11 x64, DaVinci Resolve 21.x**. Developed and load-tested on **Resolve Free 21.1.0.17** (Resolve's log confirms it loads the plugin).
* **Tested (automated, 42 tests):** matte cache and corruption handling, the C++ edge pipeline, segmentation with SAM 2, the analyze→track→correct→cache workflow, the local service over a real socket, the plugin DLL under a mock OpenFX host (describe/instantiate/render/overlay, both row-stride layouts), the Companion driven end to end, and the full chain through the *installed* product.
* **Measured (synthetic clips, RTX 4060 Laptop):** tracking quality, flicker, speed, GPU memory — see [docs/BENCHMARKS.md](docs/BENCHMARKS.md). No real-time 4K AI is claimed.
* **Unverified — needs you in Resolve:** click picking and painting on each page, alpha on the timeline, playback, project save/reload ([docs/RESOLVE_TESTING.md](docs/RESOLVE_TESTING.md) has the checklist).
* **Not implemented:** GPU (CUDA/OpenCL) render kernels — the plugin renders on the CPU (multi-threaded); SAM 2's own video-memory tracker (a flow-propagate + SAM 2 refine tracker is used instead); a dedicated hair-matting model; Studio-only Workflow-Integration panel; macOS.

### Known limitations (honest list)
* The source file path is **not available to plugins** in Resolve: you set *Source File* once per clip. Retimed/compound clips are not supported (render them first).
* Occlusion: during the occlusion the mask is flagged low-confidence and may be partial; the subject is re-acquired afterwards (synthetic occlusion test: mean IoU 0.77). Plan on correcting those frames.
* Fine hair, glass, reflections and subjects that look like their background are hard for any current model; expect manual correction.
* Person/Face auto-detect (when you have not clicked) uses simple OpenCV detectors — clicking is more reliable.
* OpenFX has no timer or progress widget: the in-Resolve status fields refresh when the panel redraws or a control is touched; use the Companion for live progress.
* Code-signing is not set up, so Windows SmartScreen may warn on the installer.

## Quick start
1. Install ([INSTALL.md](INSTALL.md)) and restart Resolve.
2. Apply *AI Cutout* to a clip, set **Source File**, press **Add Selection**, click the subject, press **Analyze**, then **Track**.
3. Set **Preview → Overlay** to inspect, paint corrections if needed, then **Output → Cutout** and composite (or **Render** for PNG sequences).

## Documentation
[ARCHITECTURE.md](ARCHITECTURE.md) · [BUILD.md](BUILD.md) · [INSTALL.md](INSTALL.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) ·
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) · [docs/BENCHMARKS.md](docs/BENCHMARKS.md) · [docs/RESOLVE_TESTING.md](docs/RESOLVE_TESTING.md)

## Repository layout
```
plugin/OFX/        OpenFX plugin (C++), vendored OpenFX 1.4 headers + Support library (BSD-3)
plugin/UI/         Companion (Tk)
core/segmentation  SegmentationEngine ABC, SAM 2 (ONNX), GrabCut fallback, Person/Object/Face/Custom modes
core/tracking      flow, tracker, appearance gate, session (keyframes, corrections)
core/temporal      temporal stabilizer
core/masking       quality / stability metrics
core/compositing   ctypes binding + PNG export of the native edge pipeline
core/native        C++: matte reader, edge pipeline, output modes (shared by plugin and Python)
core/cache         matte store (CRC'd frame files), proxy frame store
inference/         local service + client
gpu/               hardware detection, provider selection, VRAM check
models/            manifest (SHA-256) + fetch script (weights are not committed)
installer/         PyInstaller spec, Inno Setup script
tests/             automated tests, mock OpenFX host, synthetic clip generator
scripts/           build, benchmark, dev-run helpers
```

## License
MIT for this project's own source code (see `LICENSE`). Third-party components keep their own licenses: `THIRD_PARTY_LICENSES.md`.
