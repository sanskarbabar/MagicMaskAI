#!/usr/bin/env bash
# Builds build/plugin/AICutout.ofx.bundle with the user-scope llvm-mingw toolchain (no admin needed).
# CMake (plugin/OFX/CMakeLists.txt) is the canonical build; this script is the quick path.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source scripts/env.sh
OFX=plugin/OFX
# Production builds disable the Support library's host-property validation so the plugin still loads in hosts that
# omit optional properties. VALIDATE=1 builds a validating variant (build/plugin_validate) used by the mock-host tests.
if [ -n "${OUT_BUNDLE:-}" ]; then OUT="$OUT_BUNDLE/Contents/Win64"; VFLAG="${CXXEXTRA:-}"
elif [ "${VALIDATE:-0}" = "1" ]; then OUT=build/plugin_validate/AICutout.ofx.bundle/Contents/Win64; VFLAG=""; else OUT=build/plugin/AICutout.ofx.bundle/Contents/Win64; VFLAG="-DkOfxsDisableValidation"; fi
mkdir -p "$OUT"
SUP=$OFX/third_party/openfx
clang++ -std=c++17 -O2 -DNDEBUG -DWIN32 $VFLAG -shared -static -Wall -Wno-unused-parameter -Wno-deprecated-declarations \
  -I$SUP/include -I$SUP/support_include -Icore/native -I$OFX \
  $OFX/AICutout.cpp $OFX/util.cpp core/native/acm.cpp core/native/edge.cpp \
  $SUP/support_library/ofxsCore.cpp $SUP/support_library/ofxsImageEffect.cpp $SUP/support_library/ofxsInteract.cpp \
  $SUP/support_library/ofxsLog.cpp $SUP/support_library/ofxsMultiThread.cpp $SUP/support_library/ofxsParams.cpp \
  $SUP/support_library/ofxsProperty.cpp $SUP/support_library/ofxsPropertyValidation.cpp \
  -o "$OUT/AICutout.ofx"
echo "built $OUT/AICutout.ofx"
