> **Superseded.** Phase 0/1 planning document. The probes were folded into `docs/RESOLVE_TESTING.md`, which records what has been verified
> (P1: plugin loads in Resolve Free 21.1.0.17) and the checklist for what a person must still verify inside Resolve.

# Phase 1 Probes — things that must be proven inside real Resolve

The architecture depends on assumptions the SDK docs don't settle. Phase 1 builds a minimal pass-through OFX plugin
(plus logging) and answers these. Record results as PASS / FAIL / PARTIAL + Resolve edition/version in this file.

Environment to record: Resolve edition (Free/Studio), version (21.1.0.17 on the dev machine), GPU + driver.

| ID | Question | How to test | Why it matters |
|---|---|---|---|
| P1 | Does the plugin load and appear (Edit / Color / Fusion) on the installed edition? | Copy bundle to `C:\Program Files\Common Files\OFX\Plugins`, restart Resolve, open OpenFX panel | Free-vs-Studio is unresolved; sources conflict |
| P2 | Do overlay `PenDown/PenMotion/PenUp` fire on the **Edit-page** viewer, **Color-page** viewer, **Fusion** viewer? Is Draw Suite output visible on each? | Overlay that logs pen events and draws a polygon | Click-to-select; determines which page is primary |
| P3 | Can `fetchImage(t)` be called (a) inside render for t≠current, (b) from a button `changedParam`, (c) from a worker thread? How many frames can `getFramesNeeded` request? | Log per case; try 10/100/1000-frame windows | Decides whether Resolve can feed frames to analysis without file access |
| P4 | Is output alpha honoured: Edit-page track compositing, Color page (node alpha out), Fusion? Does a 2nd input clip show as "Background" on each page? | Output a half-transparent gradient alpha over a lower track | The whole product is "usable alpha" |
| P5 | With `CudaRenderSupported`: pixel format (RGBA float32?), stride, stream identity, tile/render-window behavior on 4K | Log render args; kernel writes checkerboard | GPU kernel design |
| P6 | Can the plugin create a named pipe / spawn `aicutout-service.exe` from inside Resolve (permissions, job objects, lifetime on Resolve quit)? | Spawn a stub service; echo | IPC design |
| P7 | Scripting: from `Workspace > Scripts` (and external if Studio) get selected timeline item, source file path, source in/out, speed/retime, fps, current frame | Small script printing these | Clip identity + source decoding |
| P8 | Do hidden string params survive save/reload, duplicate, copy/paste node, and different clip instances? Can we force Resolve to re-render (invalidate cache) after mattes land on disk by bumping a param, and from which thread? | Set param from `changedParam`; try from IPC-callback thread | Persistence + cache-invalidation design |

## Edition: Free (confirmed). P1 is the go/no-go gate.
Run P1 first, alone, before any other probe. Fastest possible P1 check needs no compiler: does Free show the already
installed Topaz Video AI under Color/Edit → Effects → OpenFX? (Only informative if Topaz's own licensing allows Free;
a "no" there is inconclusive, a "yes" is strong evidence.) The real answer comes from our own pass-through plugin.
P7 must be run with an *internal* script from `Workspace > Scripts` only (no external scripting on Free) and must
record which API calls return data vs `False`.

## Outcome branches
- P3 fails and P7 passes → analysis decodes source media directly (FFmpeg); unsupported codecs flagged.
- P3 and P7 both fail → analysis is driven from render calls ("capture while playing/caching"), and/or via a scripted
  Resolve render of the clip to a temp image sequence.
- P2 fails on Edit/Color → click selection only on Fusion page; Companion window hosts a frame viewer for point picking.
- P1 fails on Free → product targets Studio; documented plainly.
