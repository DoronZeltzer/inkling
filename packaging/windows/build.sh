#!/usr/bin/env bash
# Builds the Windows download folder dist/Inkling (Inkling.exe and everything it needs).
# Run inside an MSYS2 UCRT64 shell with GTK 4, PyGObject, poppler and PyInstaller installed
# (the package list is in .github/workflows/build.yml).
set -euxo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
rm -rf build dist

# the icon: Windows wants .ico
rsvg-convert -w 256 -h 256 assets/inkling.svg -o packaging/windows/inkling.png
python - <<'PY'
from PIL import Image
Image.open("packaging/windows/inkling.png").save("packaging/windows/inkling.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
PY

pyinstaller --noconfirm --clean packaging/inkling.spec
ls dist/Inkling | head -30
