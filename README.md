# MagicMaskAI

**AI rotoscoping for DaVinci Resolve, kept simple.** Open a clip, click the subject once, press Track, press Render.
A local AI cuts the subject out and follows it through the whole clip. Everything runs on your computer; no video is uploaded.

> An independent third-party tool. It is not Blackmagic Design's Magic Mask, does not use it, and makes no compatibility claim with it.

## How it works
| Step | What you do |
|---|---|
| **1. Open** | Start **AI Cutout** (Start menu, or the **Open AI Cutout** button on the Resolve effect) and open your clip. |
| **2. Click** | Left-click the subject to include it, right-click to exclude something. |
| **3. Track** | Press **Track**. The AI follows the subject through the whole clip. |
| **4. Render** | Press **Render**. In Resolve press **Update Matte** on the effect (or import the PNG sequences). |

A bad frame? Scrub to it, click on it (left = add, right = remove) and press **Track** again. Red frames on the timeline are the ones to check.

**In Resolve** the *AI Cutout* effect (Effects → OpenFX) is just a viewer for the finished matte, with six controls:
*Open AI Cutout*, *Update Matte*, *Output* (Cutout / Matte / Overlay / Checkerboard / Original), *Feather*, *Edge Shift*, *Clean Edge Colors*
(plus *Frame Offset* for trimmed clips). *Cutout* gives the clip a real alpha channel to composite over a new background.

## Status (v0.2)
* **Windows 11 x64, DaVinci Resolve 21.x.** Developed on **Resolve Free 21.1.0.17**, where Resolve's log confirms it loads the plugin.
* **Tested automatically (47 tests):** the AI workflow (click → track → fix → render), the cache, the C++ edge pipeline, the local service, the plugin DLL under a mock OpenFX host (render, every output, both buttons, bottom-up and negative-stride buffers), the app driven end to end, and the full chain through the installed build.
* **Measured** (synthetic clips, RTX 4060 Laptop): see [docs/BENCHMARKS.md](docs/BENCHMARKS.md). No real-time 4K AI is claimed.
* **Not yet verified inside Resolve:** the effect rendering on the timeline and the two buttons ([docs/RESOLVE_TESTING.md](docs/RESOLVE_TESTING.md)).

### Known limitations
* Resolve does not give plugins the clip's file path, so you pick the clip in the app, and the effect uses the **latest** clip you analysed (press **Update Matte** to pin it). Trimmed clips need **Frame Offset**; retimed/compound clips are not supported (render them first).
* During an occlusion the matte can be partial; the subject is picked up again afterwards (synthetic test: IoU 0.77). Fix those frames by clicking.
* Fine hair, glass, reflections and look-alike backgrounds are hard for any current model.
* The effect renders on the CPU (about 22–30 ms per 1080p frame, more at 4K). There are no GPU render kernels; the AI itself runs on the GPU.
* The installer is unsigned, so Windows may show a SmartScreen warning.

## Install
See [INSTALL.md](INSTALL.md). Build from source: [BUILD.md](BUILD.md). Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
Design notes: [ARCHITECTURE.md](ARCHITECTURE.md). Licenses of everything it uses: [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Repository layout
```
plugin/OFX/        the Resolve effect (C++), vendored OpenFX 1.4 headers + Support library (BSD-3)
plugin/UI/         the AI Cutout app (Tk)
core/segmentation  SAM 2 (ONNX Runtime) and a model-free fallback
core/tracking      optical-flow tracking, session (keyframes, fixes)
core/native        C++: matte reader and edge pipeline (shared by the plugin and Python)
core/cache         matte cache, proxy frames
inference/         local service + client
installer/         PyInstaller spec, Inno Setup script
tests/ scripts/    automated tests, mock OpenFX host, build scripts
```

## License
MIT for this project's own source code (see `LICENSE`). Third-party components keep their own licenses: `THIRD_PARTY_LICENSES.md`.
