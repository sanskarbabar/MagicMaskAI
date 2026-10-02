# Installing MagicMaskAI (AI Cutout)

## Requirements
* Windows 11 x64.
* DaVinci Resolve 21.x. Tested on **Resolve Free 21.1.0.17**.
* A DirectX 12 GPU (NVIDIA, AMD or Intel) is strongly recommended. Without one the AI runs on the CPU, roughly **60× slower** (measured ~15 s vs ~0.25 s per frame for the image encoder) — only practical for very short clips. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
* About 1 GB of disk. No internet is needed to run, and **no video leaves your computer**.

## Install
1. Run `AICutout-Setup-0.2.0.exe` and accept the administrator prompt (the effect goes into the shared OpenFX folder).
2. **Restart Resolve.**

What it adds (nothing of Resolve's own is touched):

| What | Where |
|---|---|
| Resolve effect | `C:\Program Files\Common Files\OFX\Plugins\AICutout.ofx.bundle` |
| App + local AI service | `C:\Program Files\AICutout` |
| AI models (SAM 2 tiny + small) | `C:\ProgramData\AICutout\models` |
| Launcher setting | `C:\ProgramData\AICutout\install.ini` |
| Your clips' analysis (created on first use) | `%LOCALAPPDATA%\AICutout\cache` |

## Use it
1. Start **AI Cutout** from the Start menu, or put the **AI Cutout** effect on a clip in Resolve (Effects → OpenFX → AI Cutout) and press **Open AI Cutout**.
2. **Open clip…**, then **click the subject** (left = include, right = exclude).
3. Press **Track**. Check the result with the *Overlay* / *Cutout* view. To fix a frame, click on it and press **Track** again.
4. Press **Render**.
5. In Resolve, on the effect press **Update Matte**, set **Output** to **Cutout**, and put the clip over your new background. (Or import the `alpha` / `cutout` PNG sequences that Render wrote next to your clip.)

Trimmed clip? Set **Frame Offset** on the effect to the first frame of the original file that the clip uses.

## Optional: High Quality model
The installer includes the Draft/Balanced models. For the larger model:
`"C:\Program Files\AICutout\aicutout-service.exe" --fetch-models base_plus` (downloads over HTTPS and verifies a SHA-256).

## Uninstall
*Settings → Apps → AI Cutout → Uninstall.* It removes what it installed and stops the service, and asks whether to also delete the analysis cache.
