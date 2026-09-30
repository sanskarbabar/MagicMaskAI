# Building AI Cutout (Windows 11 x64)

## Prerequisites
| Tool | Used for | Notes |
|---|---|---|
| Python 3.12 (64-bit) | AI service, Companion, tests | any 3.10+ should work; the installer freezes it |
| C++17 compiler | OFX plugin + native core | **either** MSVC Build Tools 2022 (canonical, needs admin to install) **or** llvm-mingw 22 (no admin: `winget install MartinStorsjo.LLVM-MinGW.UCRT --scope user`). The repo's scripts use llvm-mingw. |
| CMake ≥ 3.20 | canonical plugin build | `winget install Kitware.CMake` |
| Inno Setup 6 | installer | `winget install JRSoftware.InnoSetup` |
| Git Bash | build scripts (`scripts/*.sh`) | ships with Git for Windows |
| DaVinci Resolve 21.x | manual testing | the OpenFX headers are vendored, Resolve is *not* needed to build |

## One-time setup
```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # note: opencv-python must be <5 (see requirements.txt)
python models/fetch_models.py tiny small          # SAM 2 ONNX weights (Apache-2.0), hash-verified. add base_plus for High Quality
```
NVIDIA users may prefer `onnxruntime-gpu` (CUDA provider) instead of `onnxruntime-directml`; the service picks CUDA
automatically when present (`gpu/devices.py`). DirectML works on NVIDIA, AMD and Intel GPUs.

## Build
```bash
bash scripts/build_native.sh        # build/native/aicutout_core.dll  (edge pipeline, used by Python: Render/export/Companion preview)
bash scripts/build_plugin.sh        # build/plugin/AICutout.ofx.bundle  (production: host validation off)
bash scripts/build_host.sh          # mock OpenFX host + validating plugin variant (tests only)
```
Canonical CMake build (same sources):
```bash
cmake -S . -B build/cmake -G "MinGW Makefiles" -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build build/cmake -j
```
With MSVC: `cmake -S . -B build/msvc -G "Visual Studio 17 2022" -A x64 && cmake --build build/msvc --config Release`.

## Test
```bash
.venv/Scripts/python.exe -m pytest tests -q
```
| Suite | What it proves | Needs |
|---|---|---|
| `test_core.py` | cache format, corruption handling, stabilizer, VRAM message | — |
| `test_native.py` | C++ edge pipeline (shift, feather, smooth, decontaminate, composite), Python↔C++ format parity | native DLL |
| `test_session.py` / `test_service.py` | analyze → track → cache → correct workflow, over a real socket | — (GrabCut fallback) |
| `test_sam.py` | SAM 2 click / negative point / box / multi-object | model weights |
| `test_ofx_host.py` | the **real plugin DLL** under a mock OpenFX host: describe/instantiate, render (both row strides), overlay clicks | `build_host.sh` |
| `test_companion.py` | the Companion driven programmatically (hidden Tk window) | a desktop session |
| `test_e2e_installed.py` | host → *installed* plugin → auto-started *installed* service → SAM 2 → render | installed per-user build |

Quality/stability benchmarks: `python -m tests.bench_tracking --engine sam:tiny` and `python scripts/bench.py` (results in `docs/BENCHMARKS.md`).

## Installer
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1          # release: dist\AICutout-Setup-0.1.0.exe (needs admin to *install*)
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -DevUser # per-user variant for testing the installer logic without admin
```
The script builds the native parts, runs the tests, freezes the service and Companion with PyInstaller
(`installer/aicutout.spec`), and compiles `installer/AICutout.iss`. It refuses to package a failing tree.

## Developing against Resolve without installing (no admin)
`scripts/dev_run_resolve.ps1` starts Resolve with `OFX_PLUGIN_PATH` pointing at `build/plugin`, so the freshly built plugin is loaded
(verified to work on Resolve 21.1 Free) and the service is started from the repo's Python. Resolve's log is at
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\logs\davinci_resolve.log`; search it for `OFX: loading com.aicutout.AICutout`.

## Updating for a new Resolve version
Resolve-specific knowledge lives in three places only: the vendored OpenFX headers (`plugin/OFX/third_party/openfx`, refresh from
`%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\OpenFX`), the install paths (`docs/RESOLVE_TESTING.md`,
`installer/AICutout.iss`), and the version matrix in `docs/RESOLVE_TESTING.md`. The plugin uses only OpenFX 1.x APIs.

## Debugging
* Plugin log: `%LOCALAPPDATA%\AICutout\logs\plugin.log`; service log: `%LOCALAPPDATA%\AICutout\logs\service.log`.
* `MOCK_TRACE=1` makes the mock host print every host property the plugin asks for and does not have.
* Build a debug plugin (`OUT_BUNDLE=build/plugin_debug/AICutout.ofx.bundle CXXEXTRA=-DDEBUG_BUILD bash scripts/build_plugin.sh`) to make the OpenFX Support library print its exceptions.
