#!/usr/bin/env bash
# Sets Inkling up for the current user on any Linux computer with a desktop:
#   - checks that the helpers it needs are there, and says what to install if not
#   - adds Inkling to the application launcher (with its icon and the file types it opens)
#   - optionally starts the pen watcher (double-tap the pen to open a canvas) at every login
#
#   ./install.sh              everything
#   ./install.sh --no-pen     only the launcher entry
#
# Nothing here depends on who you are or where this folder is: it uses the folder it is run from.
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$(command -v python3 || true)"
[ -n "$python" ] || { echo "python3 is needed."; exit 1; }

echo "Checking helpers…"
missing=0
need() { # what, check command, how to get it
    if ! eval "$2" >/dev/null 2>&1; then echo "  missing: $1 ($3)"; missing=1; fi
}
need "GTK 4 for Python"      "$python -c \"import gi; gi.require_version('Gtk','4.0')\""  "Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 python3-gi-cairo"
need "PDF pages and text"    "command -v pdftoppm && command -v pdftotext"               "Debian/Ubuntu: sudo apt install poppler-utils"
need "pypdf"                 "$python -c 'import pypdf'"                                 "Debian/Ubuntu: sudo apt install python3-pypdf   (or: pip install --user pypdf)"
need "pycairo"               "$python -c 'import cairo'"                                 "Debian/Ubuntu: sudo apt install python3-cairo"
if [ "$missing" = 1 ]; then
    echo "Install the missing pieces above, then run this again. (Fedora: dnf install python3-gobject gtk4 poppler-utils python3-pypdf python3-cairo)"
    exit 1
fi
echo "  all there. Optional: pip install --user pyhanko  (digital signatures)."

apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mimes="${XDG_DATA_HOME:-$HOME/.local/share}/mime/packages"
mkdir -p "$apps" "$icons" "$mimes"

cp "$here/assets/inkling.svg" "$icons/inkling.svg"

cat > "$mimes/inkling.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-inkling">
    <comment>Inkling note</comment>
    <glob pattern="*.inkling"/>
    <generic-icon name="inkling"/>
  </mime-type>
</mime-info>
XML

cat > "$apps/local.inkling.canvas.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Inkling
Comment=Draw, write and mark up documents on a boundless canvas
Exec=$python $here/inkling.py canvas %U
Icon=inkling
Terminal=false
StartupNotify=true
StartupWMClass=local.inkling.canvas
Categories=Graphics;Office;
MimeType=application/x-inkling;x-scheme-handler/inkling;application/pdf;image/png;image/jpeg;image/gif;image/webp;image/bmp;application/vnd.openxmlformats-officedocument.wordprocessingml.document;application/vnd.openxmlformats-officedocument.presentationml.presentation;application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;application/vnd.oasis.opendocument.text;application/vnd.oasis.opendocument.presentation;application/vnd.oasis.opendocument.spreadsheet;application/msword;application/rtf;
DESKTOP

command -v update-mime-database >/dev/null && update-mime-database "$(dirname "$mimes")" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
echo "Added Inkling to your application launcher."

if [ "$1" != "--no-pen" ] && command -v systemctl >/dev/null; then
    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit"
    cat > "$unit/inkling.service" <<UNIT
[Unit]
Description=Inkling: double-tap the pen to open a drawing canvas
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$python $here/inkling.py daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now inkling.service && echo "The pen watcher is running (it starts at every login)."
fi
echo "Done. Notes are kept in $( "$python" -c "import sys; sys.path.insert(0, '$here'); import inkling; print(inkling.NOTES_DIR)" 2>/dev/null || echo "Documents/Inkling" )."
