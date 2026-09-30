# Testing inside DaVinci Resolve

Resolve has no headless plugin-test mode and this project has no way to click through Resolve's GUI automatically, so this file separates
**what has been verified** from **what you need to check on your machine**.

## Verified so far (Resolve **Free 21.1.0.17**, Windows 11, RTX 4060 Laptop)
| # | Question | Result | Evidence |
|---|---|---|---|
| P1 | Does Resolve Free load a third-party OFX plugin? | **PASS (load).** | Resolve's log records `OpenFX \| OFX: loading com.aicutout.AICutout` on startup with `OFX_PLUGIN_PATH` pointing at our bundle, and no OpenFX error follows. The same log shows `OFX: loading com.TopazLabs.VideoEnhance` for the already-installed Topaz plugin. |
| — | Does `OFX_PLUGIN_PATH` work for development without admin rights? | **Yes** | same run |
| — | Does the plugin obey the OFX contract (describe, both contexts, instantiate, render, overlay)? | **Yes, against a mock host** | `tests/test_ofx_host.py` (14 tests, validation on) |
| — | Does the whole chain work? | **Yes, against a mock host** | `tests/test_e2e_installed.py`: mock host → installed plugin → auto-started installed service → SAM 2 → alpha |

**Not verified inside Resolve** (needs a person at the screen): everything below.

## Checklist to run in Resolve
Install the release build, or for development run `scripts\dev_run_resolve.ps1`. Record PASS/FAIL/notes and the Resolve edition/version.

| ID | Test | How | Expected | If it fails |
|---|---|---|---|---|
| P1b | Plugin appears | *Color → Effects → OpenFX*; *Edit → Effects → OpenFX*; *Fusion → Effects → OpenFX* | "AI Cutout" listed (group "AI Cutout") | Check Resolve log; see TROUBLESHOOTING |
| P2 | Viewer overlay events | Apply the effect, press **Add Selection**, click in the viewer on the Edit, Color and Fusion pages | a green marker appears at the click; the *Selection* readout / status text changes | click picking will need the Companion on that page |
| P3 | Status text on the viewer | after a click | yellow "AI Cutout: …" banner top-left | overlay drawing (Draw Suite) unsupported → use the Companion for status |
| P4 | Alpha reaches the timeline | Edit page: put the clip on V2 over a different clip on V1, Output = **Cutout** | V1 visible through the background of V2 | Color page: route the node's alpha output; Fusion: use MergeAlpha/Alpha output |
| P4b | *Composite → Background Clip* | Fusion page, connect a second input | background shows through | use *Solid Color* / checker |
| P5 | Analyze/Track from inside the plugin | set *Source File*, click subject, **Analyze**, **Track** | Status group shows state/frame/confidence/ETA (updates when the panel redraws or a control is touched — OpenFX has no timer) | use the Companion for live progress |
| P6 | Render cache invalidation | after Track finishes, the viewer updates | mask appears without touching anything (plugin bumps a hidden *revision* parameter) | nudge any control once |
| P7 | Persistence | save the project, reopen | selection points, *Source File*, *Matte Set* still set; mask still shows | file a bug with the log |
| P8 | Copy/paste the node to another clip | | settings copy; *Matte Set* must be re-linked for the other clip | expected: mattes are per clip |
| P9 | Playback | play 1080p / 4K with the mask | no stalls (render only reads cached mattes) | report timings from `plugin.log` |
| P10 | Paint correction | *Paint Mode* → Add Mask, drag on the viewer | stroke turns into a manual keyframe (orange in the Companion timeline) | use the Companion's paint |

## Known Resolve-specific unknowns
* Whether Resolve delivers **pen events for overlays on the Edit-page viewer** (Fusion and Color pages are the documented OpenFX overlay hosts).
* How Resolve maps the plugin's frame `time` for trimmed clips → **Frame Offset** exists for that.
* Whether a second (**Background**) input clip is shown outside Fusion.

## Install locations used
| | Path |
|---|---|
| OFX plugins (Resolve scans this) | `C:\Program Files\Common Files\OFX\Plugins` — plus any folders in `OFX_PLUGIN_PATH` |
| Resolve scripts (unused) | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts` |
| Workflow Integration plugins (Studio only, unused) | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins` |

## Version matrix
| Resolve | Edition | Result |
|---|---|---|
| 21.1.0.17 | Free | plugin **loads** (P1). P1b–P10 pending user verification |
