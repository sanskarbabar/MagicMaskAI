# Troubleshooting

Logs: `%LOCALAPPDATA%\AICutout\logs\service.log` (the AI) and `plugin.log` (the Resolve effect).
Resolve's own log: `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\logs\davinci_resolve.log`.

| Symptom | Likely cause | What to do |
|---|---|---|
| "AI Cutout" is not in *Effects → OpenFX* | Resolve was open during install | Restart Resolve. Check that `C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle\Contents\Win64\AICutout.ofx` exists, and search Resolve's log for `OFX: loading com.aicutout.AICutout`. |
| The effect shows the original, no cutout | No matte for this clip yet, or it points at another clip | Open the app, track and render the clip, then press **Update Matte** on the effect. |
| "No matte yet…" after **Update Matte** | Nothing was analysed on this computer | Do steps 1–4 in the app first. |
| The cutout is shifted in time | Trimmed clip | Set **Frame Offset** to the first frame of the original file the clip uses. Retimed or compound clips are not supported: render them to a new file first. |
| **Open AI Cutout** says it is not installed | `C:\ProgramData\AICutout\install.ini` is missing | Re-run the installer. |
| "GPU memory is insufficient for … mode" | Not enough free GPU memory (Resolve uses it too) | Close other GPU programs. |
| Tracking is very slow | The AI is running on the CPU | The app's first message shows "Inference backend: DirectML (GPU)" or "CPU". Update your GPU driver. |
| "No SAM 2 model found. Using the classical (non-AI) engine" | Models missing | Reinstall, or copy the `.onnx` files into `C:\ProgramData\AICutout\models` (see INSTALL.md). The fallback is far less accurate. |
| Some frames are wrong | Occlusion, blur, subject leaving the frame | Red frames on the timeline are the uncertain ones. Scrub to one, click on it (left = add, right = remove), then press **Track** again. |
| The edge is too hard or soft | | Use **Feather** and **Edge Shift**; **Clean Edge Colors** removes the old background colour from the edge. |
| The service did not start | | Run `"C:\Program Files\AICutout\aicutout-service.exe" --verbose` from a terminal to see the error. |

## Environment variables (advanced)
`AICUTOUT_CACHE` (where mattes are stored), `AICUTOUT_STATE` (logs), `AICUTOUT_MODELS` (extra model folder), `AICUTOUT_DEVICE` (`auto|cuda|dml|cpu`),
`AICUTOUT_ENGINE=grabcut` (force the model-free engine), `AICUTOUT_APP_CMD` (what the effect's button launches).

## Reporting a problem
Attach both logs, your Resolve version, your GPU and driver, and what the app's message box says.
