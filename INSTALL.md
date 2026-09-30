# Installing AI Cutout

## Requirements
* Windows 11 x64 (Windows 10 64-bit should work, untested).
* DaVinci Resolve 21.x. **Tested edition: Resolve Free 21.1.0.17** (third-party OpenFX plugins are loaded by Free — see `docs/RESOLVE_TESTING.md`). Studio is expected to work the same way.
* A GPU is strongly recommended. Supported through DirectML: NVIDIA, AMD and Intel GPUs with DirectX 12. Measured on an RTX 4060 Laptop (8 GB): see `docs/BENCHMARKS.md`. Without a usable GPU the service falls back to the CPU, which is roughly **60× slower** for the image encoder (measured on this machine: ~15 s vs ~0.25 s per frame for the tiny model) — usable only for very short clips.
* ~1 GB disk for the program and Draft/Balanced models; +350 MB for High Quality.
* No internet connection is needed to run. **No video or image ever leaves your computer** — the service listens on 127.0.0.1 only.

## Install
1. Run `AICutout-Setup-0.1.0.exe` (administrator rights are requested: the plugin goes into the shared OpenFX folder).
2. Components: *OFX plugin*, *AI service + Companion*, *SAM 2 models*. Choose "Without model files" to skip the models and add them later:
   `"C:\Program Files\AICutout\aicutout-service.exe" --fetch-models small base_plus` (downloads over HTTPS and verifies SHA-256), or copy the `.onnx` files into `%PROGRAMDATA%\AICutout\models`.
3. The installer detects Resolve and tells you whether it found it. It only ever adds files to:

| What | Where |
|---|---|
| OFX plugin | `C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle` |
| Service + Companion | `C:\Program Files\AICutout` |
| Models | `C:\ProgramData\AICutout\models` |
| Service launcher config | `C:\ProgramData\AICutout\install.ini` |
| Analysis cache (created on first use) | `%LOCALAPPDATA%\AICutout\cache` |

It never modifies Resolve's own files. **Restart Resolve** after installing.

## First use
1. In Resolve open *Effects → OpenFX → AI Cutout* (Color, Edit and Fusion pages) and apply it to a clip (Color page: add a node, then apply).
2. In the plugin, set **Source File** to the original media file of that clip (Resolve does not tell plugins the file path).
3. Press **Add Selection**, click the subject in the viewer (use **Remove Selection** for areas to exclude), press **Analyze**, then **Track**.
4. Switch **Preview** to *Overlay* to check the mask; use **Paint Mode** to correct, then **Propagate Correction**.
5. Set **Output** to *Cutout* (real alpha channel) and composite over your background, or *Composite* for a quick preview.
6. **Render** writes full-resolution 16-bit PNG sequences (`alpha/` and `cutout/`) you can also import as mattes.

Prefer a bigger window with a timeline and progress bar? Open **AI Cutout Companion** from the Start menu: same service, same cache, works
alongside the plugin (open the clip there, analyze/track, then press **Link to Last Analyzed Clip** in the plugin).

## Uninstall
*Settings → Apps → AI Cutout → Uninstall.* It removes everything it installed (plugin, program, models, launcher config) and stops the service.
It asks whether to also delete the analysis cache in `%LOCALAPPDATA%\AICutout`.
