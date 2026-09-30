# Third-Party Licenses

Status legend: **Verified** = read from the component's own metadata/LICENSE in this build environment;
**Web** = confirmed from the project's public page during research (re-read the LICENSE file before a commercial
release); **To verify** = believed correct, not yet confirmed. This file is not legal advice — get a legal review
before commercial distribution.

## A. Shipped in the installer

| Component | Version | License | Commercial use / redistribution | Attribution / notes | Status |
|---|---|---|---|---|---|
| **SAM 2 model weights** (`sam2_hiera_tiny/small/base_plus` encoder+decoder, ONNX) | as downloaded 2026-09-30, SHA-256 in `models/manifest.json` | Apache-2.0 (Meta SAM 2 checkpoints; the ONNX export repo also declares Apache-2.0) | Yes / Yes | Keep license + attribution "SAM 2 © Meta Platforms, Inc." Meta's demo fonts (SIL OFL) are **not** shipped. Weights are converted, not retrained. | Web (Meta repo + HF model card) |
| **OpenFX 1.4 headers + Support library** (vendored in `plugin/OFX/third_party/openfx`) | 1.4 (from the Resolve 21.1 SDK) | BSD-3-Clause (The Open Effects Association) | Yes / Yes | Copyright notice retained in `third_party/openfx/LICENSE` and file headers | Verified (LICENSE + SPDX header read) |
| **Python runtime** (frozen by PyInstaller) | 3.12 | PSF License | Yes / Yes | includes Tcl/Tk (BSD-style) | Verified (python.org terms) |
| **NumPy** | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (wheel metadata) | Yes / Yes | wheel bundles OpenBLAS (BSD-3) and a gfortran runtime; see the wheel's LICENSES_bundled | Verified (metadata) |
| **OpenCV (opencv-python)** | 4.14.0.94 (pinned <5) | Apache-2.0 (metadata) | Yes / Yes | The wheel bundles **FFmpeg under LGPL-2.1** and other libs: see `cv2/LICENSE-3RD-PARTY.txt` in the package. FFmpeg is dynamically linked (separate DLLs in `_internal\cv2`), so users can replace it; keep the LGPL notice and offer source on request. | Verified (metadata + bundled notice file exists) |
| **ONNX Runtime (DirectML build)** | 1.24.4 | MIT (metadata) | Yes / Yes | ships `onnxruntime.dll`, `DirectML.dll` | Verified (metadata) |
| **DirectML.dll** | bundled with the above | Microsoft redistributable terms | Redistribution allowed as part of ONNX Runtime | keep Microsoft notice | To verify |
| **protobuf / flatbuffers / sympy / mpmath / packaging** (ONNX Runtime dependencies, only if PyInstaller collects them) | 7.36 / 25.12 / 1.14 / 1.3 / 26.3 | BSD-3 / Apache-2.0 / BSD / BSD / Apache-2.0 OR BSD-2 | Yes / Yes | | Verified (metadata) |
| **llvm-mingw runtime** statically linked into `AICutout.ofx` and `aicutout_core.dll` | 22.1.8 (2026-06-16) | libc++ / libunwind: Apache-2.0 WITH LLVM-exception; mingw-w64 CRT: ZPL-2.1 / public domain; winpthreads: MIT-style | Yes / Yes | | To verify |

## B. Build/test tools (not shipped)

| Tool | License | Note |
|---|---|---|
| PyInstaller 6.22.3 + hooks-contrib | GPLv2-or-later **with a special exception that allows the frozen program to be distributed under any license** | The bootloader exception applies to the produced executables. Verified (metadata). |
| Inno Setup 6 | Inno Setup License (free, incl. commercial) | installer compiler |
| CMake, llvm-mingw, pytest (MIT) | BSD-3 / Apache-2.0-with-exceptions / MIT | |
| Meta SAM sample images `tests/data/*.jpg` | Apache-2.0 (from facebookresearch/segment-anything) | test data only |

## C. Candidate components evaluated and **not** used

| Component | License | Decision |
|---|---|---|
| SAM 3 / 3.1 | Custom "SAM License" (2025-11-19): commercial use allowed; redistribution requires including the license; bans military/nuclear/ITAR/weapons uses; no reverse engineering | Not used. Would pass those restrictions on to users; needs legal review first. (Web) |
| EdgeTAM | Apache-2.0 | Possible future Draft-tier engine (needs ONNX export + benchmark). (Web) |
| MobileSAM | MIT vs Apache-2.0 (sources disagree) | Possible click-preview engine; read the repo LICENSE first. (To verify) |
| Cutie | MIT (code) | Possible tracker; weights terms to verify. (Web) |
| MODNet | Apache-2.0 | Possible person-matting refinement. (Web) |
| BiRefNet | MIT (code); some derived weights (RMBG-2.0) are non-commercial | Possible high-res refinement; verify each weight file. (Web) |
| **Robust Video Matting** | **GPL-3.0** | **Excluded** (copyleft). (Web) |
| **MatAnyone** | **NTU S-Lab License** (non-commercial) | **Excluded.** (Web) |
| FastSAM | believed AGPL-3.0 | Excluded until verified. (To verify) |

## D. Rules for adding a component
1. Read the LICENSE file and the weights' model card; record license id, commercial use, redistribution, attribution.
2. No copyleft (GPL/AGPL/static LGPL) in shipped closed-source binaries.
3. No non-commercial or field-restricted weights in the default install.
4. Record SHA-256 of every shipped weight file in `models/manifest.json` (`models/fetch_models.py` enforces it).
