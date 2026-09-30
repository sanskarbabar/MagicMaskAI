#!/usr/bin/env bash
# Builds build/native/aicutout_core.dll (Python binding + tests) with the user-scope llvm-mingw toolchain.
set -e
cd "$(dirname "$0")/.."
source scripts/env.sh
mkdir -p build/native
clang++ -std=c++17 -O2 -DNDEBUG -shared -static -Wall -Wextra -o build/native/aicutout_core.dll core/native/acm.cpp core/native/edge.cpp core/native/capi.cpp
echo "built build/native/aicutout_core.dll"
