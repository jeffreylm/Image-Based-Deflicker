#!/bin/sh
set -eu
cd "$(dirname "$0")"
bundle=build/ImageBasedDeflickerProbe.ofx.bundle
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
xcrun clang++ -std=c++20 -O2 -mmacosx-version-min=13.0 \
  -Wall -Wextra -Wpedantic -Werror -fvisibility=hidden -bundle \
  -Ivendor/openfx src/plugin.cpp \
  -o "$bundle/Contents/MacOS/ImageBasedDeflickerProbe.ofx"
cp Info.plist "$bundle/Contents/Info.plist"
cp vendor/openfx/LICENSE.md "$bundle/Contents/Resources/OpenFX-LICENSE.md"
cp NOTICE.md "$bundle/Contents/Resources/NOTICE.md"
codesign --force --sign - "$bundle"
printf '%s\n' "Built $bundle"
