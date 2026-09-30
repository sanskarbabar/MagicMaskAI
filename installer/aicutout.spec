# PyInstaller spec: builds dist/AICutout/ containing aicutout-service.exe and aicutout-companion.exe that share one
# set of runtime libraries (Python, numpy, OpenCV, ONNX Runtime + DirectML). Run from the repo root:
#   .venv\Scripts\python.exe -m PyInstaller installer\aicutout.spec --noconfirm --distpath dist --workpath build\pyi
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.getcwd())
core_dll = os.path.join(ROOT, "build", "native", "aicutout_core.dll")
binaries = [(core_dll, ".")] if os.path.isfile(core_dll) else []
binaries += collect_dynamic_libs("onnxruntime")
datas = collect_data_files("cv2")           # includes the Haar face cascade used by Face mode
datas += [(os.path.join(ROOT, "models", "manifest.json"), "models")]   # SHA-256 pins for --fetch-models

hidden = ["onnxruntime.capi.onnxruntime_pybind11_state", "models.fetch_models"]
excludes = ["matplotlib", "scipy", "pandas", "torch", "torchvision", "IPython", "pytest", "notebook", "sphinx"]

a_service = Analysis([os.path.join(ROOT, "inference", "server_main.py")], pathex=[ROOT], binaries=binaries, datas=datas,
                     hiddenimports=hidden, excludes=excludes, noarchive=False)
a_comp = Analysis([os.path.join(ROOT, "plugin", "UI", "companion_main.py")], pathex=[ROOT], binaries=binaries, datas=datas,
                  hiddenimports=hidden, excludes=excludes, noarchive=False)
MERGE((a_service, "aicutout-service", "aicutout-service"), (a_comp, "aicutout-companion", "aicutout-companion"))

pyz_s = PYZ(a_service.pure)
pyz_c = PYZ(a_comp.pure)
exe_s = EXE(pyz_s, a_service.scripts, [], exclude_binaries=True, name="aicutout-service", console=False, icon=None)
exe_c = EXE(pyz_c, a_comp.scripts, [], exclude_binaries=True, name="aicutout-companion", console=False, icon=None)
coll = COLLECT(exe_s, a_service.binaries, a_service.datas, exe_c, a_comp.binaries, a_comp.datas, name="AICutout")
