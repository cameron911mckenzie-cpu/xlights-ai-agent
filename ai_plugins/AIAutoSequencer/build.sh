#!/usr/bin/env bash
# Build the AI Auto Sequencer xLights plugin with plain g++/clang++ (no CMake needed).
# Output: ./build/AIAutoSequencer.so   (rename to .dll on Windows / .dylib on macOS)
set -euo pipefail
cd "$(dirname "$0")"

CXX="${CXX:-g++}"
OUT=build
mkdir -p "$OUT"

"$CXX" -std=c++17 -fPIC -O2 -Wall -Wextra \
    -I. -Ithird_party/xlights \
    -shared AIAutoSequencerService.cpp \
    -o "$OUT/AIAutoSequencer.so"

echo "built $OUT/AIAutoSequencer.so"
echo "install: copy it into <xLights>/ai_plugins/AIAutoSequencer.so"
echo "  (the ai_plugins/ folder sits next to the xLights executable)"
