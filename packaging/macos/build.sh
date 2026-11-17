#!/usr/bin/env bash
# Builds the macOS download dist/Inkling.app (and Inkling-macos.zip).
# Needs Homebrew's gtk4, pygobject3, py3cairo, poppler, librsvg and adwaita-icon-theme, and a Python venv that can
# see them (the workflow sets this up).
set -euxo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
rm -rf build dist

# the icon: macOS wants .icns, made from the same drawing
iconset="$(mktemp -d)/inkling.iconset"
mkdir -p "$iconset"
for size in 16 32 64 128 256 512; do
    rsvg-convert -w $size -h $size assets/inkling.svg -o "$iconset/icon_${size}x${size}.png"
    rsvg-convert -w $((size * 2)) -h $((size * 2)) assets/inkling.svg -o "$iconset/icon_${size}x${size}@2x.png"
done
iconutil -c icns "$iconset" -o packaging/macos/inkling.icns

pyinstaller --noconfirm --clean packaging/inkling.spec
codesign --force --deep --sign - dist/Inkling.app || true      # a free "ad hoc" signature: the app still opens via right-click > Open
ditto -c -k --keepParent dist/Inkling.app dist/Inkling-macos.zip
ls dist
