# Measured performance and quality

**Read this first.** All numbers below were measured on **one machine** with **synthetic** test clips that have exact ground truth
(`tests/synth.py`: a textured "person" sprite on a noisy textured background, with camera pan/zoom/rotation, fast motion, motion blur,
an occluder, low light, a similar-colour background and hair-like strands). Synthetic clips are useful for *regression testing and
relative comparison*; they are **not** a substitute for real footage, where results will differ (usually the hard cases are harder).
No claim below should be read as "works on your footage". Reproduce with `python scripts/bench.py` and `python -m tests.bench_tracking`.

| | |
|---|---|
| Machine | Windows 11, 20 logical CPU cores, NVIDIA GeForce RTX 4060 Laptop GPU (8 GB), Intel UHD iGPU, driver 560.94 |
| Inference | ONNX Runtime 1.24.4 DirectML provider, SAM 2 (ONNX export) |
| Clip size | 640×360, 40–50 frames per scenario; the AI analyses a proxy (Draft 512 / Balanced 768 / High 1024 px long edge), so source resolution does not change analysis cost |
| Metrics | IoU and boundary-F vs ground truth; *flicker* = excess frame-to-frame alpha change vs the truth's own change (0 = as stable as the truth) |

## 1. Models compared (mean over the 10 scenarios)
| Model | Mean IoU | Analysis+tracking speed | GPU memory (+) | CPU load | Model load |
|---|---|---|---|---|---|
| SAM 2 **tiny** (Draft) | 0.921 | **5.8 frames/s** | +1.3 GB | 1.7 cores | 0.8 s |
| SAM 2 **small** (Balanced) | 0.909 | 5.3 frames/s | +1.3 GB | 1.5 cores | 0.6 s |
| SAM 2 **base_plus** (High) | 0.911 | 3.4 frames/s | +2.5 GB | 1.3 cores | 0.8 s |

*GPU memory* = increase of the GPU's global `memory.used` during the run (includes DirectML overhead; other programs, e.g. Resolve, move that number too).
Extrapolated (not measured): a 1000-frame clip needs ≈ 3 min (tiny) to ≈ 5 min (base_plus) of analysis, once.
On the CPU (no GPU) the image encoder alone takes ≈ 15 s per frame (tiny, measured on a 1800×1200 photo) vs ≈ 0.25 s on this GPU — not practical.

## 2. Per scenario, IoU / boundary-F (tracking from a single click on frame 0)
| Scenario | tiny | small | base_plus | Notes |
|---|---|---|---|---|
| static camera | 0.958 / 0.956 | 0.948 / 0.939 | 0.967 / 0.967 | |
| moving camera (pan) | 0.964 / 0.960 | 0.958 / 0.951 | 0.973 / 0.971 | |
| zoom | 0.955 / 0.957 | 0.943 / 0.930 | 0.964 / 0.964 | |
| rotation | 0.955 / 0.955 | 0.948 / 0.939 | 0.967 / 0.967 | |
| fast movement | 0.969 / 0.963 | 0.953 / 0.932 | 0.973 / 0.957 | |
| motion blur | 0.943 / 0.952 | 0.925 / 0.884 | 0.938 / 0.921 | was 0.33 before the drift fixes (see §5) |
| **occlusion** (34 px opaque bar crosses the subject) | **0.773** / 0.762 | **0.716** / 0.677 | **0.812** / 0.829 | min IoU 0.27–0.31: frames *during* the occlusion are partial; flagged low-confidence; subject re-acquired afterwards |
| low light | 0.956 / 0.958 | 0.942 / 0.923 | 0.964 / 0.959 | |
| **similar-colour background** | **0.802** / 0.536 | **0.835** / 0.650 | **0.606** / 0.334 | weakest case; the larger model is *worse* here |
| hair-like strands | 0.931 / 0.852 | 0.927 / 0.856 | 0.943 / 0.880 | boundary-F is low: fine strands are not resolved by a segmentation model |

Temporal stability: flicker ≤ 0.0012 in every scenario (≈ as stable as the ground truth); largest single-frame area change ≤ 3 % outside the occlusion scenario.
A bigger model is not automatically better on this suite (tiny ≈ small ≈ base_plus on mean IoU), so **Draft/tiny is a sensible default on this class of content**;
High Quality mainly buys cleaner edges on easy footage. Model choice should be validated on your own material.

## 3. Render side (OFX plugin / export, CPU, 20-thread machine)
Full-resolution edge pipeline from the cached analysis-resolution matte, subject filling roughly half the frame (larger subjects cost more, small subjects less):

| Frame | Cutout, default edge (Balanced) | Full pipeline (shift + feather + smooth + refine + decontaminate + spill) Draft / Balanced / High |
|---|---|---|
| 1080p | **31 ms** (≈ 32 fps) | 119 / 128 / 138 ms (≈ 8 fps) |
| 4K | **116 ms** (≈ 9 fps) | 461 / 490 / 540 ms (≈ 2 fps) |

So: at 1080p the default cutout plays back at about real time on this CPU, but heavy edge settings and 4K do **not** — Resolve's render cache (or **Render** to PNG sequences) is
the way to play those back. **GPU render kernels are not implemented**; they are the main remaining performance work. No real-time 4K claim is made.
Video decode + proxy build ran at ≈ 160 frames/s for 1080p H.264 → 768 px (synthetic clip, this CPU).

## 4. What was not measured
Real footage; multiple simultaneous people; transparent objects and reflections; shadows; CUDA-provider inference; AMD/Intel GPUs; memory of Resolve running at the same time; playback inside Resolve.

## 5. History that matters (why the tracker looks the way it does)
Bugs found by this suite and fixed: (1) motion-blur drift (0.33 → 0.94): box re-expansion every frame + optical flow dragged by the blur → robust affine flow, tighter box, expected-area guard;
(2) occlusion collapse (0.37 → 0.77): the tracker latched onto the occluder → clean-reference chain, colour-appearance gate, constant-velocity prior, appearance-based re-acquisition;
(3) similar-colour background is still weak (0.6–0.84).
