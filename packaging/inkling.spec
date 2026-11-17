# PyInstaller recipe for the Windows and macOS downloads (run from the repository root: pyinstaller packaging/inkling.spec).
# The Linux version is the source folder itself (see install.sh).
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))        # SPECPATH: the folder of this file (set by PyInstaller)
ICON = os.path.join(ROOT, "packaging", "windows", "inkling.ico") if sys.platform == "win32" else os.path.join(
    ROOT, "packaging", "macos", "inkling.icns")

# helper programs the app starts: they travel inside the package, in a "bin" folder
binaries = []
for tool in ("pdftoppm", "pdftotext", "pdfinfo", "pdfunite", "pdfseparate", "pdftocairo", "pdfimages"):
    path = shutil.which(tool)
    if path:
        binaries.append((path, "bin"))

gi_versions = {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0", "Graphene": "1.0", "Pango": "1.0", "PangoCairo": "1.0",
               "GdkPixbuf": "2.0", "Gio": "2.0", "GLib": "2.0", "GObject": "2.0"}

a = Analysis(
    [os.path.join(ROOT, "inkling.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[(os.path.join(ROOT, "assets"), "assets"), (os.path.join(ROOT, "selftest.py"), ".")],
    hiddenimports=["selftest", "converter", "converter_ui", "cairo", "pypdf", "cryptography", "yaml", "lxml", "PIL"]
    + [f"gi.repository.{name}" for name in gi_versions],
    hooksconfig={"gi": {"module-versions": gi_versions, "icons": ["Adwaita", "hicolor"], "themes": ["Adwaita"],
                        "languages": ["en"]}},
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Inkling", console=False, icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, name="Inkling")

if sys.platform == "darwin":
    app = BUNDLE(
        coll, name="Inkling.app", icon=ICON, bundle_identifier="app.inkling.Inkling",
        info_plist={
            "CFBundleName": "Inkling", "CFBundleDisplayName": "Inkling", "NSHighResolutionCapable": True,
            "CFBundleDocumentTypes": [
                {"CFBundleTypeName": "Inkling note", "CFBundleTypeExtensions": ["inkling"], "CFBundleTypeRole": "Editor"},
                {"CFBundleTypeName": "PDF", "CFBundleTypeExtensions": ["pdf"], "CFBundleTypeRole": "Editor"},
            ],
        },
    )
