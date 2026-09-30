#!/usr/bin/env bash
# Builds the mock OpenFX host (test tool) and the validation-enabled plugin variant it drives.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"; source scripts/env.sh
mkdir -p build/host
clang++ -std=c++17 -O1 -DWIN32 -Wall -Wno-unused-parameter -Iplugin/OFX/third_party/openfx/include tests/host/mock_host.cpp -o build/host/mock_host.exe -static
VALIDATE=1 bash scripts/build_plugin.sh
echo "built build/host/mock_host.exe"
