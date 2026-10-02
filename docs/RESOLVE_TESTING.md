# Testing inside DaVinci Resolve

Resolve has no headless plugin-test mode and this project cannot click through Resolve's GUI automatically, so this file separates
**what has been verified** from **what a person needs to check**.

## Verified (Resolve **Free 21.1.0.17**, Windows 11)
| Question | Result | Evidence |
|---|---|---|
| Does Resolve Free load a third-party OpenFX plugin? | **Yes** | Resolve's log shows `OpenFX \| OFX: loading com.aicutout.AICutout` (v0.1 and the installed build) with no error after it. |
| Does `OFX_PLUGIN_PATH` work for development without admin rights? | **Yes** | same run |
| Does the effect obey the OFX contract (describe, both contexts, instantiate, render, buttons)? | **Yes, against a mock host** | `tests/test_ofx_host.py` (13 tests, host validation on) |
| Does the whole chain work? | **Yes, against a mock host** | `tests/test_e2e_installed.py`: installed service tracks a clip with SAM 2 → installed plugin renders the alpha |

The v0.2 effect (6 controls, no viewer overlay) has the same load behaviour as v0.1 (same entry points, no new dependencies — it now imports only Windows system DLLs); re-confirm with the checklist below.

## Checklist to run in Resolve
Install the release build, or for development run `scripts\dev_run_resolve.ps1`. Record PASS/FAIL and the Resolve edition/version.

| ID | Test | Expected |
|---|---|---|
| R1 | Edit/Color/Fusion page: Effects → OpenFX → **AI Cutout** | listed under the group "AI Cutout"; applies to a clip |
| R2 | Press **Open AI Cutout** on the effect | the app window opens |
| R3 | In the app: open the same clip, click the subject, Track, Render. In Resolve press **Update Matte** | the status line says "Matte linked (N frames)" and the viewer shows the cutout |
| R4 | **Output = Cutout**, clip on V2 over a different clip on V1 (Edit page) | V1 is visible through V2's background (real alpha) |
| R5 | Color page: apply to a node | Cutout alpha is available from the node's alpha output |
| R6 | **Output = Overlay / Matte / Checkerboard / Original** | each view changes accordingly; Original is the untouched clip |
| R7 | **Feather**, **Edge Shift**, **Clean Edge Colors** | the edge softens / grows / loses the old background colour |
| R8 | Save the project, reopen | the effect still shows the cutout (it remembers which matte it uses) |
| R9 | Play 1080p / 4K | no stalls (render only reads the cached matte); heavy 4K settings may be slow on CPU |
| R10 | Trimmed clip | wrong timing → set **Frame Offset**; correct after |

Known unknowns: whether Resolve's render cache needs **Update Matte** (or any control nudge) to refresh after a re-render; how Resolve numbers
frames for trimmed clips (hence **Frame Offset**).

## Install locations
| | Path |
|---|---|
| OFX plugins (Resolve scans this) | `C:\Program Files\Common Files\OFX\Plugins`, plus any folder in `OFX_PLUGIN_PATH` |

## Version matrix
| Resolve | Edition | Result |
|---|---|---|
| 21.1.0.17 | Free | effect **loads**. R1–R10 pending a person in Resolve |
