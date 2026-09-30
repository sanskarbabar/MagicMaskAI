# Troubleshooting

Logs: `%LOCALAPPDATA%\AICutout\logs\plugin.log` (the OFX plugin) and `service.log` (the AI service).
Resolve's own log: `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\logs\davinci_resolve.log`.

| Symptom | Likely cause | What to do |
|---|---|---|
| "AI Cutout" is not in *Effects → OpenFX* | Resolve was running during install, or the bundle is missing | Restart Resolve. Check `C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle\Contents\Win64\AICutout.ofx` exists. Search Resolve's log for `OFX: loading com.aicutout.AICutout`. Some Resolve versions cache the plugin list: *Preferences → General → Reset* is not needed, a restart is. |
| **"Set 'Source File' to the original media file of this clip first."** | Resolve does not expose the media path to plugins | Set the *Source File* parameter (Source group) to the original file. For trimmed clips also set *Frame Offset* (= first used source frame). |
| "AI Cutout service is not installed…" | `%PROGRAMDATA%\AICutout\install.ini` missing | Re-run the installer; for development set `AICUTOUT_SERVICE_CMD` to `python -m inference.server` (with the repo as working directory). |
| "The AI Cutout service did not start" | model/driver problem or port/antivirus block | Open `service.log`. Run `aicutout-service.exe --verbose` from a terminal to see the error. |
| Selection does nothing / no clicks recorded | *Click Tool* is Off, or the host does not deliver overlay events on this page | Press **Add Selection** first. If Resolve does not deliver pen events on the page you are using, try the Fusion page or use the **Companion** for picking — this is one of the Resolve behaviours that must be confirmed on your version (`docs/RESOLVE_TESTING.md`, probe P2). |
| Viewer shows the original but not the mask | No matte yet, or the plugin instance is linked to a different set | Press **Analyze** then **Track**. Check *Matte Set*; press **Link to Last Analyzed Clip**. |
| Mask appears on frame N but is shifted in time | Trimmed clip / retime | Adjust **Frame Offset**. Retimed (speed-changed) and compound clips are not supported: render the retime first. |
| **"GPU memory is insufficient for High Quality mode. Try Balanced mode or Draft mode."** | Not enough free VRAM (Resolve also uses it) | Choose Balanced or Draft, close other GPU applications. |
| "Model file missing" / "No SAM 2 model found. Using the classical (non-AI) engine" | Models not installed | Install them (`INSTALL.md`). The classical GrabCut fallback is only a stop-gap and is far less accurate. |
| Analysis is very slow | Running on CPU | Service log / Companion header shows the backend ("DirectML (GPU)" or "CPU"). Update the GPU driver; on CPU expect ~15 s per frame. |
| Low-confidence frames (red in the Companion timeline) | Occlusion, subject leaving the frame, heavy blur, lookalike background | Scrub to the first bad frame, paint the correction (Paint Mode) or add include/exclude points and **Confirm**, then **Propagate Correction**. |
| Mask edge is too soft/hard | Edge controls | *Edge Refinement* snaps to image edges; *Smooth* removes jaggies; *Edge Shift* grows/shrinks; *Decontaminate Edge* + *Spill Suppression* remove background colour. |
| Cache seems stale after replacing the source file | The cache key contains the file's path/size/mtime/content sample | It invalidates automatically; press **Reset** to force a clean start. Cache root can be moved with the `AICUTOUT_CACHE` environment variable. |
| Corrupted cache | Power loss during a write | Bad frame files are detected by CRC, deleted and recomputed automatically. Delete the folder in `%LOCALAPPDATA%\AICutout\cache\<set>` to start over. |

## Environment variables
`AICUTOUT_CACHE` (matte cache root), `AICUTOUT_STATE` (logs / service.json), `AICUTOUT_MODELS` (extra model folder),
`AICUTOUT_DEVICE` (`auto|cuda|dml|cpu`), `AICUTOUT_ENGINE=grabcut` (force the model-free engine), `AICUTOUT_SERVICE_CMD` (how the plugin starts the service).

## Reporting a problem
Attach `plugin.log`, `service.log`, the Resolve version/edition, GPU model and driver version, and what the plugin's **Status → Message** field says.
