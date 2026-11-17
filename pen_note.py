#!/usr/bin/env python3
"""Pen Note: double-tap the pen on the screen to open a boundless canvas.

  pen_note.py daemon                 watch the pen, open the canvas on a double tap
  pen_note.py canvas [--screenshot FILE] [--open NAME]
                                     open the canvas directly (also used by the daemon)

Each save writes, into the Obsidian vault: a PNG of everything on the canvas, a markdown
note that embeds it, and a hidden editable copy so the note can be opened and continued.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

VAULT = Path.home() / "Obsidian" / "Main"
NOTES_DIR = Path(os.environ.get("PEN_NOTE_DIR") or VAULT / "Pen Notes")
PROJECT_DIR = NOTES_DIR / ".pennote"   # hidden: Obsidian ignores dot-folders
SELF = Path(__file__).resolve()

# --- double-tap tuning (my defaults, tweak here) -----------------------------
TAP_MAX_DURATION = 0.30    # a tap is a touch shorter than this (seconds)
TAP_MAX_MOVE = 0.03        # ...that moves less than this fraction of the screen
DOUBLE_TAP_GAP = 0.50      # max time between the two taps (seconds)
DOUBLE_TAP_DIST = 0.08     # max distance between the two taps (screen fraction)
COOLDOWN = 1.5             # ignore taps for this long after opening (seconds)


# =============================================================================
# Daemon: watch the pen
# =============================================================================
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y = 0, 1
BTN_TOOL_PEN, BTN_TOUCH = 0x140, 0x14A
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def find_pen_device():
    """Return /dev/input/eventN of the stylus digitizer, or None."""
    try:
        text = Path("/proc/bus/input/devices").read_text()
    except OSError:
        return None
    for block in text.split("\n\n"):
        name = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=(.*)", block)
        if not name or not handlers:
            continue
        if re.search(r"stylus|\bpen\b", name.group(1), re.I):
            ev = re.search(r"event\d+", handlers.group(1))
            if ev:
                return f"/dev/input/{ev.group(0)}"
    return None


def abs_max(fd, axis):
    nr = (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + axis)
    info = struct.unpack("6i", fcntl.ioctl(fd, nr, b"\0" * 24))
    return max(info[2], 1)  # (value, min, max, fuzz, flat, res)


def take_screenshot():
    """Screenshot via the desktop portal. Returns a temp PNG path or None."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    loop = GLib.MainLoop()
    sender = bus.get_unique_name()[1:].replace(".", "_")
    token = f"pennote{os.getpid()}x{int(time.time())}"
    req = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    result = {}

    def on_resp(conn, snd, obj, iface, sig, params):
        code, res = params.unpack()
        result["code"], result["res"] = code, res
        loop.quit()

    sub = bus.signal_subscribe(None, "org.freedesktop.portal.Request", "Response",
                               req, None, Gio.DBusSignalFlags.NONE, on_resp)
    try:
        opts = {"handle_token": GLib.Variant("s", token),
                "interactive": GLib.Variant("b", False)}
        bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                      "org.freedesktop.portal.Screenshot", "Screenshot",
                      GLib.Variant("(sa{sv})", ("", opts)), None,
                      Gio.DBusCallFlags.NONE, -1, None)
        GLib.timeout_add_seconds(10, loop.quit)
        loop.run()
    except Exception as e:  # noqa: BLE001
        log("screenshot failed:", e)
        return None
    finally:
        bus.signal_unsubscribe(sub)

    if result.get("code") != 0:
        log("screenshot denied or failed:", result)
        return None
    src = Path(unquote(urlparse(result["res"]["uri"]).path))
    run_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    dst = run_dir / f"pen-note-shot-{int(time.time())}.png"
    shutil.move(str(src), str(dst))  # keep ~/Pictures clean
    return dst


def canvas_running():
    """True if any Pen Note canvas is open, however it was started (double tap, dock, terminal)."""
    me = os.getpid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == me:
            continue
        try:
            args = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"pen_note.py") for a in args) and b"canvas" in args:
            return True
    return False


def run_daemon():
    ignore_until = 0.0

    while True:
        dev = find_pen_device()
        if not dev:
            log("no pen device found, retrying in 5s")
            time.sleep(5)
            continue
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log(f"cannot open {dev}: {e}; retrying in 5s")
            time.sleep(5)
            continue
        log("watching", dev)
        xmax, ymax = abs_max(fd, ABS_X), abs_max(fd, ABS_Y)

        x = y = 0
        touching = False
        down_t = 0.0
        down_pos = (0, 0)
        last_tap = None  # (time, x, y)

        try:
            while True:
                select.select([fd], [], [])
                try:
                    data = os.read(fd, EVENT_SIZE * 64)
                except BlockingIOError:
                    continue
                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _us, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                    if etype == EV_ABS:
                        if code == ABS_X:
                            x = value
                        elif code == ABS_Y:
                            y = value
                    elif etype == EV_KEY and code == BTN_TOUCH:
                        now = time.monotonic()
                        if value == 1:
                            touching, down_t, down_pos = True, now, (x, y)
                        elif value == 0 and touching:
                            touching = False
                            moved = max(abs(x - down_pos[0]) / xmax,
                                        abs(y - down_pos[1]) / ymax)
                            if now - down_t > TAP_MAX_DURATION or moved > TAP_MAX_MOVE:
                                last_tap = None  # a stroke, not a tap
                                continue
                            if now < ignore_until or canvas_running():
                                last_tap = None
                                continue
                            if (last_tap and now - last_tap[0] <= DOUBLE_TAP_GAP
                                    and max(abs(x - last_tap[1]) / xmax,
                                            abs(y - last_tap[2]) / ymax) <= DOUBLE_TAP_DIST):
                                last_tap = None
                                shot = take_screenshot()
                                cmd = [sys.executable, str(SELF), "canvas"]
                                if shot:
                                    cmd += ["--screenshot", str(shot)]
                                subprocess.Popen(cmd)
                                ignore_until = time.monotonic() + COOLDOWN
                            else:
                                last_tap = (now, x, y)
        except OSError as e:
            log("pen device lost:", e)
            os.close(fd)
            time.sleep(2)


# =============================================================================
# Canvas: GTK4 window with a boundless drawing surface
# =============================================================================
COLORS = [("Black", (0.08, 0.08, 0.08)), ("Blue", (0.10, 0.30, 0.85)),
          ("Red", (0.85, 0.15, 0.15)), ("Green", (0.10, 0.60, 0.25))]
MIN_ZOOM, MAX_ZOOM = 0.02, 8.0
EXPORT_MARGIN = 40          # white space around the content in the saved PNG (world pixels)
EXPORT_MAX_SIDE = 16000     # longest side of the saved PNG (pixels)
EXPORT_MAX_PIXELS = 60_000_000   # total pixels of the saved PNG
EXPORT_SCALE = 3.0          # saved PNG pixels per world pixel, reduced if that exceeds the limits

# --- slides (PowerPoint, OpenDocument, PDF) ----------------------------------
SLIDE_EXTS = {".pptx", ".ppt", ".pps", ".ppsx", ".odp", ".pdf"}
SLIDE_RENDER_WIDTH = 1400   # pixels per slide picture
SLIDE_DISPLAY_WIDTH = 900   # world pixels per slide on the surface
SLIDE_GAP_RATIO = 0.6       # blank space after each slide, as a fraction of the slide's height
MAX_SLIDES = 150
# The snap-packaged LibreOffice can only read non-hidden folders inside your home folder,
# so decks are copied into this work folder (and removed again) for conversion.
WORK_DIR = SELF.parent / "work"
# Converted decks are kept here, so adding the same file again is instant.
CACHE_DIR = Path(os.environ.get("PEN_NOTE_CACHE") or WORK_DIR / "cache")
CACHE_LIMIT = 500 * 1024 * 1024    # bytes


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", name)
    return re.sub(r"\s+", " ", name).strip().strip(".")


# One conversion at a time: two LibreOffice runs on the same profile collide and the second fails.
_convert_lock = threading.Lock()


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_cache():
    """Keep the cache under CACHE_LIMIT by deleting the least recently used decks."""
    try:
        decks = [d for d in CACHE_DIR.iterdir() if d.is_dir()]
        sizes = {d: sum(f.stat().st_size for f in d.iterdir()) for d in decks}
        total = sum(sizes.values())
        for d in sorted(decks, key=lambda d: d.stat().st_mtime):
            if total <= CACHE_LIMIT:
                break
            total -= sizes[d]
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def convert_slides(path, progress=lambda text: None, on_page=None):
    """Turn a PowerPoint/OpenDocument/PDF file into one PNG per slide.

    Slow for PowerPoint (LibreOffice needs most of a minute for a big deck), so call it off the UI
    thread. `progress(text)` reports what it is doing and `on_page(number, total, png_bytes)` is
    called for each slide, in order, as soon as that slide is ready (so slides can appear while the
    rest are still rendering). Slides are rendered in parallel, and a deck that was converted before
    comes straight from the cache. Returns the number of slides. Raises RuntimeError."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SLIDE_EXTS:
        raise RuntimeError("that isn't a PowerPoint, OpenDocument or PDF file")
    key = f"{file_sha1(path)}-{SLIDE_RENDER_WIDTH}"
    cache = CACHE_DIR / key
    if _convert_lock.locked():
        progress("Waiting for the other deck to finish…")
    with _convert_lock:
        done_marker = cache / "complete"
        if done_marker.exists():                         # converted before: instant
            total = int(done_marker.read_text())
            progress("Found this deck in the cache")
            for i in range(1, total + 1):
                if on_page:
                    on_page(i, total, (cache / f"p-{i:03d}.png").read_bytes())
            os.utime(cache)                              # most recently used
            return total

        shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True)
        WORK_DIR.mkdir(exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_DIR))
        pool = None
        try:
            src = job / f"deck{ext}"
            shutil.copyfile(path, src)
            if ext == ".pdf":
                pdf = src
            else:
                office = shutil.which("libreoffice") or shutil.which("soffice")
                if not office:
                    raise RuntimeError("LibreOffice isn't installed; export the slides as a PDF and add that instead")
                progress("Converting with LibreOffice… a big deck can take a minute (a PDF is much faster)")
                profile = WORK_DIR / "lo-profile"   # own profile: works even while LibreOffice is open
                r = subprocess.run(
                    [office, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                     "--convert-to", "pdf", "--outdir", str(job), str(src)],
                    capture_output=True, text=True, timeout=300)
                pdf = job / "deck.pdf"
                if not pdf.exists():
                    detail = (r.stdout + r.stderr).strip()[-160:]
                    raise RuntimeError("LibreOffice could not convert it" + (f": {detail}" if detail else
                                       " (it returned nothing; is another LibreOffice job running?)"))
            info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
            m = re.search(r"Pages:\s+(\d+)", info.stdout)
            total = min(int(m.group(1)), MAX_SLIDES) if m else 0
            if total == 0:
                raise RuntimeError("no pages found: " + (info.stderr.strip()[-160:] or "empty file?"))

            def render(i):
                out = cache / f"p-{i:03d}"              # -singlefile writes exactly <out>.png
                r = subprocess.run(
                    ["pdftoppm", "-png", "-singlefile", "-f", str(i), "-l", str(i),
                     "-scale-to-x", str(SLIDE_RENDER_WIDTH), "-scale-to-y", "-1", str(pdf), str(out)],
                    capture_output=True, text=True, timeout=300)
                png = Path(str(out) + ".png")
                if not png.exists():
                    raise RuntimeError(f"could not render slide {i}: {r.stderr.strip()[-120:]}")
                return png.read_bytes()

            progress(f"Rendering {total} slide{'s' if total != 1 else ''}…")
            pool = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4)))
            futures = [pool.submit(render, i) for i in range(1, total + 1)]
            for i, fut in enumerate(futures, 1):         # hand over slides in order, as each is ready
                data = fut.result()
                if on_page:
                    on_page(i, total, data)
            done_marker.write_text(str(total))
            prune_cache()
            return total
        except subprocess.TimeoutExpired:
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError("conversion took too long (over 5 minutes)")
        except BaseException:
            shutil.rmtree(cache, ignore_errors=True)    # never keep a half-finished deck
            raise
        finally:
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)
            shutil.rmtree(job, ignore_errors=True)


class Item:
    """A picture or a text box. x, y, image width and text size are in world pixels
    (1 world pixel = 1 screen pixel at zoom 1)."""
    __slots__ = ("kind", "x", "y", "width", "texture", "text", "size", "color", "png", "auto")

    def __init__(self, kind, x, y, width=400.0, texture=None, text="", size=24.0,
                 color=(0.08, 0.08, 0.08), png=None, auto=False):
        self.kind, self.x, self.y = kind, x, y
        self.width, self.texture, self.text, self.size, self.color = width, texture, text, size, color
        self.png = png        # cached PNG bytes of the picture (for saving)
        self.auto = auto      # the screenshot placed automatically: doesn't count as "content"

    def clone(self):
        return Item(self.kind, self.x, self.y, self.width, self.texture, self.text, self.size,
                    self.color, self.png, self.auto)


def run_canvas(screenshot, open_name=None, slides=None):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Pango", "1.0")
    from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

    class Canvas(Gtk.Widget):
        """Drawing surface; renders through GTK's snapshot API (no cairo bridge needed)."""
        __gtype_name__ = "PenNoteCanvas"

        def __init__(self, app):
            super().__init__(hexpand=True, vexpand=True, focusable=True)
            self.app = app

        def do_snapshot(self, snapshot):
            self.app.snapshot_view(snapshot, self.get_width(), self.get_height())

        def do_dispose(self):
            pop = getattr(self.app, "popover", None)
            if pop is not None and pop.get_parent() is self:
                pop.unparent()
            Gtk.Widget.do_dispose(self)

    class Stroke:
        __slots__ = ("color", "width", "pts")

        def __init__(self, color, width):
            self.color, self.width, self.pts = color, width, []  # pts: (x, y, pressure) in world pixels

    def texture_png(item):
        if item.png is None:
            item.png = item.texture.save_to_png_bytes().get_data()
        return item.png

    class App(Gtk.Application):
        def __init__(self):
            suffix = ".selftest" if os.environ.get("PEN_NOTE_SELFTEST") else ""
            super().__init__(application_id="local.pennote.canvas" + suffix)
            self.strokes = []
            self.items = []           # pictures and text boxes
            self.selected = None      # the item the Move tool has selected
            self.undo_stack = []
            self.redo_stack = []
            self.current = None
            self.mode = "pen"         # pen | eraser | move | text
            self._mode_lock = False
            self.gesture_tool = None  # what the gesture in progress is doing
            self.grab = None
            self.grab_snapped = False
            self.color = COLORS[0][1]
            self.pen_px = 3.5         # pen size on screen (slider value)
            self.eraser_px = 12.0     # eraser size on screen (slider value)
            self.text_px = 24.0       # new text size on screen
            self.saved_base = None    # base filename of the last save / the note that was opened
            self.dirty = False
            self.zoom = 1.0
            self.ox = self.oy = 0.0   # screen = world * zoom + (ox, oy); no limits: the surface is boundless
            self.pinching = False
            self.pointer = None
            self.text_target = None
            self.text_pos = (0.0, 0.0)
            self.text_cancel = False
            self.export_renderer = None
            self.close_warned = False
            self.importing = set()    # slide files being converted right now
            self.pending = {}         # path -> slides already shown from a deck still loading
            self.deck_state = {}      # path -> where the next slide of that deck goes
            self.msgs = []            # progress messages shown (kept for the self-test)

        # ---- window ---------------------------------------------------------
        def do_activate(self):
            win = Gtk.ApplicationWindow(application=self, title="Pen Note")
            self.win = win
            win.set_default_size(1100, 760)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            win.set_child(root)

            def new_bar():
                bar = Gtk.Box(spacing=6, margin_top=4, margin_bottom=2, margin_start=8, margin_end=8)
                root.append(bar)
                return bar

            def add_button(bar, label, cb, tip=None):
                b = Gtk.Button(label=label)
                b.connect("clicked", cb)
                if tip:
                    b.set_tooltip_text(tip)
                bar.append(b)
                return b

            # row 1: name, open, view, history, save
            bar = new_bar()
            self.name_entry = Gtk.Entry(placeholder_text="Note name (optional)", hexpand=True)
            bar.append(self.name_entry)
            add_button(bar, "Open", self.on_open, "Open a saved note to continue it")
            add_button(bar, "−", lambda *_: self.zoom_by(1 / 1.4), "Zoom out")
            add_button(bar, "+", lambda *_: self.zoom_by(1.4), "Zoom in")
            add_button(bar, "Fit", lambda *_: self.fit_all(), "Show everything on the surface")
            add_button(bar, "Undo", self.on_undo, "Undo (Ctrl+Z)")
            add_button(bar, "Redo", self.on_redo, "Redo (Ctrl+Shift+Z)")
            add_button(bar, "Clear", self.on_clear, "Remove everything")
            save = add_button(bar, "Save", lambda *_: self.save(notify=True))
            save.add_css_class("suggested-action")

            # row 2: tools
            bar = new_bar()
            group = None
            for label, rgb in COLORS:
                b = Gtk.ToggleButton(label="●")
                b.set_tooltip_text(label)
                css = Gtk.CssProvider()
                css.load_from_string(
                    "button { color: rgb(%d,%d,%d); font-size: 20px; }" %
                    tuple(int(c * 255) for c in rgb))
                b.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                if group is None:
                    group = b
                    b.set_active(True)
                else:
                    b.set_group(group)
                b.connect("toggled", self.on_color, rgb)
                bar.append(b)

            size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 15, 0.5)
            size.set_value(self.pen_px)
            size.set_draw_value(False)
            size.set_size_request(110, -1)
            size.set_tooltip_text("Pen size")
            size.connect("value-changed", lambda s: setattr(self, "pen_px", s.get_value()))
            bar.append(size)

            self.mode_btns = {}
            for name, label, tip in (("eraser", "Eraser", "Erase pen strokes"),
                                     ("move", "Move", "Select, move and resize pictures and text"),
                                     ("text", "Text", "Tap to add a text box, tap one to edit it")):
                b = Gtk.ToggleButton(label=label)
                b.set_tooltip_text(tip)
                b.connect("toggled", self.on_mode_toggled, name)
                self.mode_btns[name] = b
                bar.append(b)
                if name == "eraser":
                    esize = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 3, 40, 1)
                    esize.set_value(self.eraser_px)
                    esize.set_draw_value(False)
                    esize.set_size_request(110, -1)
                    esize.set_tooltip_text("Eraser size")
                    esize.connect("value-changed",
                                  lambda s: setattr(self, "eraser_px", s.get_value()))
                    bar.append(esize)

            add_button(bar, "Delete", self.on_delete_selected, "Delete the selected picture or text")
            add_button(bar, "Paste", self.on_paste, "Paste a picture from the clipboard (Ctrl+V)")
            add_button(bar, "Slides", self.on_slides, "Add a PowerPoint, OpenDocument or PDF file: every slide becomes a picture")
            tip = Gtk.Label(label="Drag with a finger to move around · drop pictures or slides here", hexpand=True, xalign=1)
            tip.add_css_class("dim-label")
            bar.append(tip)

            self.area = Canvas(self)
            root.append(self.area)

            info = Gtk.Box(spacing=12, margin_start=8, margin_end=8, margin_bottom=4, margin_top=2)
            self.spinner = Gtk.Spinner(visible=False)     # shown while slides are being converted
            info.append(self.spinner)
            self.tool_label = Gtk.Label(label="", xalign=0)
            self.tool_label.add_css_class("heading")
            info.append(self.tool_label)
            self.status = Gtk.Label(label="", xalign=0, hexpand=True)
            info.append(self.status)
            root.append(info)
            self.update_tool_label()

            # text-box editor
            self.popover = Gtk.Popover()
            self.popover.set_parent(self.area)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.textview = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.textview.set_size_request(280, 90)
            box.append(self.textview)
            hint = Gtk.Label(label="Ctrl+Enter or click away to finish, Esc to cancel", xalign=0)
            hint.add_css_class("dim-label")
            box.append(hint)
            self.popover.set_child(box)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.on_text_key)
            self.textview.add_controller(keys)
            self.popover.connect("closed", self.on_text_closed)

            stylus = Gtk.GestureStylus()
            stylus.connect("down", self.on_stylus_down)
            stylus.connect("motion", self.on_stylus_motion)
            stylus.connect("up", self.on_stylus_up)
            self.area.add_controller(stylus)

            drag = Gtk.GestureDrag()
            drag.set_button(1)
            drag.connect("drag-begin", self.on_drag_begin)
            drag.connect("drag-update", self.on_drag_update)
            drag.connect("drag-end", self.on_drag_end)
            self.area.add_controller(drag)
            self.drag = drag

            # fingers: pinch to zoom, two fingers or one finger to move the view
            pinch = Gtk.GestureZoom()
            pinch.connect("scale-changed", self.on_pinch)
            pinch.connect("end", lambda *_: setattr(self, "pinching", False))
            pinch.connect("cancel", lambda *_: setattr(self, "pinching", False))
            self.area.add_controller(pinch)
            self.pinch = pinch

            scroll = Gtk.EventControllerScroll(
                flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
            scroll.connect("scroll", self.on_scroll)
            self.area.add_controller(scroll)

            motion = Gtk.EventControllerMotion()
            motion.connect("motion", lambda c, x, y: setattr(self, "pointer", (x, y)))
            self.area.add_controller(motion)

            # drop pictures: files from a file manager, or image data from other apps
            drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
            drop.set_gtypes([Gdk.FileList, Gdk.Texture])
            drop.connect("drop", self.on_drop)
            self.area.add_controller(drop)

            winkeys = Gtk.EventControllerKey()
            winkeys.connect("key-pressed", self.on_window_key)
            win.add_controller(winkeys)

            win.connect("close-request", self.on_close)

            # starting content: a screenshot on white space, or a note to continue
            if screenshot and Path(screenshot).exists():
                try:
                    self.place_screenshot(Gdk.Texture.new_from_filename(screenshot))
                except GLib.Error as e:
                    log("cannot load screenshot:", e.message)
            elif open_name:
                self.open_note(open_name)

            win.present()
            self.area.grab_focus()
            if slides:
                self.add_slide_file(slides)
            if os.environ.get("PEN_NOTE_SELFTEST"):
                GLib.timeout_add(1500, self.selftest)

        def place_screenshot(self, tex):
            """Screenshot at the top-left of the view, leaving white space right and below."""
            tw, th = tex.get_width(), tex.get_height()
            width = min(720.0, 480.0 * tw / max(th, 1))
            self.items.append(Item("image", 40.0, 40.0, width=width, texture=tex, auto=True))

        # ---- tools ----------------------------------------------------------
        def set_mode(self, mode):
            self._mode_lock = True
            self.mode = mode
            for name, b in self.mode_btns.items():
                b.set_active(name == mode)
            self._mode_lock = False
            if mode != "move":
                self.selected = None
            self.update_tool_label()
            self.area.queue_draw()

        def update_tool_label(self):
            """Always show what the pen will do, so 'it doesn't draw' is never a mystery."""
            self.tool_label.set_text({
                "pen": "Tool: Draw",
                "eraser": "Tool: Eraser (pen strokes only)",
                "move": "Tool: Move (pen drags pictures and text; pick a color to draw)",
                "text": "Tool: Text (tap to place; pick a color to draw)",
            }[self.mode])

        def on_mode_toggled(self, btn, name):
            if self._mode_lock:
                return
            if btn.get_active():
                self.set_mode(name)
            elif self.mode == name:
                self.set_mode("pen")

        def on_color(self, btn, rgb):
            if btn.get_active():
                self.color = rgb
                if self.mode in ("eraser", "move"):  # picking a color means "draw"
                    self.set_mode("pen")

        # ---- undo / redo -----------------------------------------------------
        def state(self):
            return (list(self.strokes), [i.clone() for i in self.items])

        def restore(self, st):
            self.strokes = list(st[0])
            self.items = [i.clone() for i in st[1]]
            self.selected = None
            self.dirty = True
            self.area.queue_draw()

        def push_undo(self):
            """Call before any change: remember the canvas so Undo can restore it."""
            self.undo_stack.append(self.state())
            del self.undo_stack[:-50]
            self.redo_stack.clear()  # a new action ends the redo history
            self.dirty = True

        def on_undo(self, *_):
            if self.undo_stack:
                self.redo_stack.append(self.state())
                self.restore(self.undo_stack.pop())

        def on_redo(self, *_):
            if self.redo_stack:
                self.undo_stack.append(self.state())
                self.restore(self.redo_stack.pop())

        def on_clear(self, *_):
            if self.strokes or self.items:
                self.push_undo()
                self.strokes = []
                self.items = []
                self.selected = None
                self.area.queue_draw()

        def on_delete_selected(self, *_):
            if self.selected in self.items:
                self.push_undo()
                self.items = [i for i in self.items if i is not self.selected]
                self.selected = None
                self.area.queue_draw()

        def on_window_key(self, ctrl, keyval, keycode, state):
            ctrl_down = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if ctrl_down and keyval in (Gdk.KEY_v, Gdk.KEY_V):
                self.on_paste()
            elif ctrl_down and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
                self.on_redo() if shift else self.on_undo()
            elif ctrl_down and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
                self.on_redo()
            elif keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
                self.on_delete_selected()
            else:
                return False
            return True

        # ---- view: zoom and pan (the surface has no edges) --------------------
        def canvas_size(self):
            w, h = self.area.get_width(), self.area.get_height()
            if w < 1 or h < 1:
                w, h = 1100, 640
            return w, h

        def to_world(self, x, y):
            """Widget pixel -> world pixel, undoing zoom and pan."""
            return (x - self.ox) / self.zoom, (y - self.oy) / self.zoom

        def set_view(self, zoom, ox, oy):
            self.zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
            self.ox, self.oy = ox, oy
            self.area.queue_draw()

        def zoom_by(self, factor, px=None, py=None):
            w, h = self.canvas_size()
            if px is None:
                px, py = self.pointer if self.pointer else (w / 2, h / 2)
            nz = min(max(self.zoom * factor, MIN_ZOOM), MAX_ZOOM)
            cx, cy = self.to_world(px, py)               # world point under (px, py) stays put
            self.set_view(nz, px - cx * nz, py - cy * nz)

        def fit_all(self):
            if not (self.strokes or self.items):
                self.set_view(1.0, 0, 0)
                return
            bx, by, bw, bh = self.content_bounds()
            w, h = self.canvas_size()
            z = min(max(min(w / bw, h / bh, 4.0), MIN_ZOOM), MAX_ZOOM)
            self.set_view(z, (w - bw * z) / 2 - bx * z, (h - bh * z) / 2 - by * z)

        def on_pinch(self, g, scale):
            ok, cx, cy = g.get_bounding_box_center()
            if not ok:
                return
            if not self.pinching:  # first update of this pinch: remember the starting view
                self.pinching = True
                self.pinch_base = self.zoom / max(scale, 0.01)
                self.pinch_pt = self.to_world(cx, cy)
            nz = min(max(self.pinch_base * scale, MIN_ZOOM), MAX_ZOOM)
            # keep the content under the fingers' midpoint under the fingers: zoom + pan together
            self.set_view(nz, cx - self.pinch_pt[0] * nz, cy - self.pinch_pt[1] * nz)

        def on_scroll(self, ctrl, dx, dy):
            state = ctrl.get_current_event_state()
            if state & Gdk.ModifierType.CONTROL_MASK:
                self.zoom_by(1.15 ** (-dy))
            else:
                self.set_view(self.zoom, self.ox - dx * 40, self.oy - dy * 40)
            return True

        # ---- pictures --------------------------------------------------------
        def add_image(self, texture, wx=None, wy=None):
            """Put a picture on the surface, centred on widget point (wx, wy) (default: view centre)."""
            w, h = self.canvas_size()
            tw, th = texture.get_width(), texture.get_height()
            if tw < 1 or th < 1:
                return
            width = 0.4 * w / self.zoom                       # 40% of the view width
            if width * th / tw > 0.8 * h / self.zoom:         # very tall picture: fit the height
                width = 0.8 * h / self.zoom * tw / th
            cx, cy = self.to_world(w / 2 if wx is None else wx, h / 2 if wy is None else wy)
            self.push_undo()
            item = Item("image", cx - width / 2, cy - width * th / tw / 2, width=width, texture=texture)
            self.items.append(item)
            self.selected = item           # stays in the current tool, so the pen keeps drawing
            self.status.set_text("Picture added. Choose Move to drag it or resize it by its corner.")
            self.area.queue_draw()

        def on_drop(self, target, value, x, y):
            textures, decks = [], []
            if isinstance(value, Gdk.Texture):
                textures.append(value)
            elif isinstance(value, Gdk.FileList):
                for f in value.get_files():
                    p = f.get_path()
                    if p and Path(p).suffix.lower() in SLIDE_EXTS:
                        decks.append(p)
                        continue
                    try:
                        textures.append(Gdk.Texture.new_from_file(f))
                    except GLib.Error as e:
                        log("not a picture:", p, e.message)
            for i, tex in enumerate(textures):
                self.add_image(tex, x + 30 * i, y + 30 * i)
            for p in decks:
                self.add_slide_file(p)
            if not textures and not decks:
                self.status.set_text("That isn't a picture or slide file I can open.")
            return bool(textures or decks)

        # ---- slides ----------------------------------------------------------
        def on_slides(self, *_):
            dlg = Gtk.FileDialog(title="Add slides")
            flt = Gtk.FileFilter()
            flt.set_name("Slides (PowerPoint, OpenDocument, PDF)")
            for ext in sorted(SLIDE_EXTS):
                flt.add_suffix(ext[1:])
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(flt)
            dlg.set_filters(store)
            dlg.set_default_filter(flt)
            dlg.open(self.win, None, self.on_slides_chosen)

        def on_slides_chosen(self, dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return                                       # cancelled
            if f and f.get_path():
                self.add_slide_file(f.get_path())

        def show_progress(self, text):
            self.msgs.append(text)
            self.status.set_text(text)
            return False

        def add_slide_file(self, path, done=None):
            """Convert a deck in the background; its slides appear one by one as they are ready."""
            name = Path(path).name
            if path in self.importing:
                self.status.set_text(f"“{name}” is already being opened. Please wait.")
                if done:
                    done()
                return
            self.importing.add(path)
            self.spinner.set_visible(True)
            self.spinner.start()
            self.show_progress(f"Opening “{name}”…")
            label = f"“{name}”: "

            def work():
                try:
                    n = convert_slides(
                        path,
                        lambda t: GLib.idle_add(self.show_progress, label + t),
                        lambda i, total, data: GLib.idle_add(self.slide_ready, path, name, i, total, data))
                    err = None
                except Exception as e:  # noqa: BLE001
                    n, err = 0, str(e)
                GLib.idle_add(self.slides_done, path, name, n, err, done)
            threading.Thread(target=work, daemon=True).start()

        def slide_ready(self, path, name, i, total, data):
            """One more slide arrived: show it straight away, below the previous one.

            Slides stay 'pending' (visible, but not yet part of the note) until the whole deck is
            there; only then do they become one undo step. That keeps Undo consistent even if you
            draw while the rest of the deck is still loading."""
            try:
                st = self.deck_state.get(path)
                if st is None:                                   # first slide: decide where the deck goes
                    w, h = self.canvas_size()
                    if self.strokes or self.items:               # keep clear of what's there: start to its right
                        bx, by, bw, bh = self.content_bounds()
                        x0, y0 = bx + bw - EXPORT_MARGIN + 200, by + EXPORT_MARGIN
                    else:
                        x0, y0 = self.to_world(40, 40)
                    st = self.deck_state[path] = {"x": x0, "y": y0}
                    self.pending[path] = []
                    z = min(1.0, (w - 80) / SLIDE_DISPLAY_WIDTH)
                    self.set_view(z, 40 - x0 * z, 40 - y0 * z)   # first slide at the top-left of the view
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                height = SLIDE_DISPLAY_WIDTH * tex.get_height() / tex.get_width()
                self.pending[path].append(
                    Item("image", st["x"], st["y"], width=SLIDE_DISPLAY_WIDTH, texture=tex, png=data))
                st["y"] += height * (1 + SLIDE_GAP_RATIO)
                self.show_progress(f"“{name}”: slide {i} of {total} ready" + ("" if i < total else "."))
                self.area.queue_draw()
            except Exception as e:  # noqa: BLE001
                log("slide display failed:", e)
            return False

        def slides_done(self, path, name, n, err, done):
            self.importing.discard(path)
            self.deck_state.pop(path, None)
            slides = self.pending.pop(path, [])
            if not self.importing:
                self.spinner.stop()
                self.spinner.set_visible(False)
            if err:
                log("slides failed:", name, err)
                self.status.set_text(f"Could not open “{name}”: {err}")
            elif slides:
                self.push_undo()                                  # the whole deck is one undo step
                self.items.extend(slides)
                self.selected = None
                self.area.queue_draw()
                self.status.set_text(f"Added {len(slides)} slide{'s' if len(slides) != 1 else ''} from “{name}”. "
                                     "Move down to see them all and write in the space between them.")
            if done:
                done()
            return False

        def on_paste(self, *_):
            cb = self.area.get_clipboard()
            formats = cb.get_formats()
            if formats.contain_gtype(Gdk.Texture):
                cb.read_texture_async(None, self.on_paste_texture)
            elif formats.contain_gtype(Gdk.FileList):
                cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, self.on_paste_files)
            else:
                self.status.set_text("The clipboard has no picture.")

        def on_paste_texture(self, cb, res):
            try:
                tex = cb.read_texture_finish(res)
            except GLib.Error as e:
                self.status.set_text(f"Could not paste: {e.message}")
                return
            if tex:
                self.add_image(tex)

        def on_paste_files(self, cb, res):
            try:
                files = cb.read_value_finish(res).get_files()
            except GLib.Error as e:
                self.status.set_text(f"Could not paste: {e.message}")
                return
            for f in files:
                p = f.get_path()
                if p and Path(p).suffix.lower() in SLIDE_EXTS:
                    self.add_slide_file(p)
                    continue
                try:
                    self.add_image(Gdk.Texture.new_from_file(f))
                except GLib.Error:
                    pass

        # ---- text boxes ------------------------------------------------------
        def make_layout(self, item):
            layout = self.area.create_pango_layout(item.text)
            fd = Pango.FontDescription.from_string("Sans")
            fd.set_absolute_size(max(item.size, 1.0) * Pango.SCALE)
            layout.set_font_description(fd)
            return layout

        def item_box(self, item):
            """(x, y, width, height) of an item in world pixels."""
            if item.kind == "image":
                tw, th = item.texture.get_width(), item.texture.get_height()
                return item.x, item.y, item.width, item.width * th / tw
            pw, ph = self.make_layout(item).get_pixel_size()
            return item.x, item.y, max(pw, 8), max(ph, 8)

        def hit_item(self, cx, cy, kinds=("text", "image")):
            pad = 4 / self.zoom
            for kind in kinds:                       # text is drawn above pictures
                for it in reversed(self.items):
                    if it.kind != kind:
                        continue
                    bx, by, bw, bh = self.item_box(it)
                    if bx - pad <= cx <= bx + bw + pad and by - pad <= cy <= by + bh + pad:
                        return it
            return None

        def text_begin(self, x, y):
            wx, wy = self.to_world(x, y)
            self.text_target = self.hit_item(wx, wy, kinds=("text",))
            self.text_pos = (wx, wy)
            self.textview.get_buffer().set_text(self.text_target.text if self.text_target else "")
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            self.popover.set_pointing_to(rect)
            self.text_cancel = False
            self.popover.popup()
            self.textview.grab_focus()

        def on_text_key(self, ctrl, keyval, keycode, state):
            if keyval == Gdk.KEY_Escape:
                self.text_cancel = True
                self.popover.popdown()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
                self.popover.popdown()
                return True
            return False

        def on_text_closed(self, pop):
            buf = self.textview.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            target, self.text_target = self.text_target, None
            if self.text_cancel:
                return
            self.commit_text(target, self.text_pos, text)
            self.area.grab_focus()

        def commit_text(self, target, pos, text):
            """Create a text box (target None) or change/delete an existing one."""
            if target is not None and target in self.items:
                if not text:
                    self.push_undo()
                    self.items = [i for i in self.items if i is not target]
                elif text != target.text:
                    self.push_undo()
                    target.text = text
            elif text:
                self.push_undo()
                self.items.append(Item("text", pos[0], pos[1], text=text,
                                       size=self.text_px / self.zoom, color=self.color))
            self.area.queue_draw()

        # ---- input -----------------------------------------------------------
        def begin(self, x, y, pressure, erase):
            self.area.grab_focus()
            self.erasing = False
            self.current = None
            if not erase and self.mode == "move":
                self.gesture_tool = "move"
                self.move_begin(x, y)
                return
            if not erase and self.mode == "text":
                self.gesture_tool = "text"
                self.text_begin(x, y)
                return
            wx, wy = self.to_world(x, y)
            self.push_undo()
            self.erasing = erase or self.mode == "eraser"
            self.gesture_tool = "erase" if self.erasing else "draw"
            if self.erasing:
                self.erase_at(wx, wy)
            else:
                # divide by zoom so the pen keeps the same size on screen whatever the zoom
                self.current = Stroke(self.color, self.pen_px * 1.1 / self.zoom)
                self.current.pts.append((wx, wy, pressure))
                self.strokes.append(self.current)
            self.area.queue_draw()

        def extend(self, x, y, pressure):
            tool = self.gesture_tool
            if tool == "move":
                self.move_extend(x, y)
                return
            if tool not in ("draw", "erase"):
                return
            wx, wy = self.to_world(x, y)
            if tool == "erase":
                self.erase_at(wx, wy)
            elif self.current:
                self.current.pts.append((wx, wy, pressure))
            self.area.queue_draw()

        def finish(self):
            self.current = None
            self.grab = None

        def erase_at(self, wx, wy):
            r = self.eraser_px * 1.1 / self.zoom  # same size on screen whatever the zoom
            self.strokes = [s for s in self.strokes
                            if not any(math.hypot(px - wx, py - wy) < r for px, py, _ in s.pts)]

        # Move tool: select a picture or text, drag to move, drag the corner square to resize
        def move_begin(self, x, y):
            cx, cy = self.to_world(x, y)
            self.grab, self.grab_snapped = None, False
            sel = self.selected if self.selected in self.items else None
            if sel is not None:
                bx, by, bw, bh = self.item_box(sel)
                if math.hypot(cx - (bx + bw), cy - (by + bh)) < 18 / self.zoom:
                    self.grab = ("resize", sel, bw, sel.size)
            if self.grab is None:
                it = self.hit_item(cx, cy)
                self.selected = it
                if it is not None:
                    self.grab = ("move", it, cx - it.x, cy - it.y)
            self.area.queue_draw()

        def move_extend(self, x, y):
            if not self.grab:
                return
            cx, cy = self.to_world(x, y)
            kind, it, a, b = self.grab
            if not self.grab_snapped:        # first real change: remember the state for Undo
                self.push_undo()             # runs before the change below, so it stores the old position
                self.grab_snapped = True
            if kind == "move":
                it.x, it.y = cx - a, cy - b
            elif it.kind == "image":
                it.width = max(cx - it.x, 12.0)
            else:  # resize text: scale the font by how far the corner moved
                ratio = max(cx - it.x, 8) / max(a, 1)
                it.size = max(b * ratio, 4.0)
            self.area.queue_draw()

        def tool_is_eraser(self, gesture):
            tool = gesture.get_device_tool()
            return bool(tool and tool.get_tool_type() == Gdk.DeviceToolType.ERASER)

        def pressure(self, gesture):
            ok, p = gesture.get_axis(Gdk.AxisUse.PRESSURE)
            return p if ok else 0.5

        def on_stylus_down(self, g, x, y):
            self.begin(x, y, self.pressure(g), self.tool_is_eraser(g))

        def on_stylus_motion(self, g, x, y):
            self.extend(x, y, self.pressure(g))

        def on_stylus_up(self, g, x, y):
            self.finish()

        def drag_source(self):
            dev = self.drag.get_device()
            return dev.get_source() if dev is not None else None

        def on_drag_begin(self, g, x, y):
            src = self.drag_source()
            self.drag_ok = src == Gdk.InputSource.MOUSE          # mouse draws
            self.drag_pan = src == Gdk.InputSource.TOUCHSCREEN   # one finger moves the view
            self.drag_start = (x, y)
            self.pan_start = (self.ox, self.oy)
            if self.drag_ok:
                self.begin(x, y, 0.5, False)

        def on_drag_update(self, g, dx, dy):
            if self.drag_ok:
                self.extend(self.drag_start[0] + dx, self.drag_start[1] + dy, 0.5)
            elif self.drag_pan and not self.pinching:
                self.set_view(self.zoom, self.pan_start[0] + dx, self.pan_start[1] + dy)

        def on_drag_end(self, g, dx, dy):
            if self.drag_ok:
                self.finish()

        # ---- drawing ---------------------------------------------------------
        def snapshot_view(self, snapshot, w, h):
            rect = Graphene.Rect().init(0, 0, w, h)
            white = Gdk.RGBA()
            white.red = white.green = white.blue = white.alpha = 1.0
            snapshot.append_color(white, rect)
            snapshot.push_clip(rect)
            snapshot.save()
            snapshot.translate(Graphene.Point().init(self.ox, self.oy))
            snapshot.scale(self.zoom, self.zoom)
            self.draw_content(snapshot)
            snapshot.restore()
            snapshot.pop()

        def draw_content(self, snapshot, export=False):
            """Everything on the surface, in world pixels (the caller sets up the transform)."""
            for it in self.items:                                  # pictures, under the pen
                if it.kind == "image":
                    snapshot.append_texture(it.texture, Graphene.Rect().init(*self.item_box(it)))
            if not export:                                         # slides still arriving
                for slides in self.pending.values():
                    for it in slides:
                        snapshot.append_texture(it.texture, Graphene.Rect().init(*self.item_box(it)))
            for s in self.strokes:
                rgba = Gdk.RGBA()
                rgba.red, rgba.green, rgba.blue = s.color
                rgba.alpha = 1.0
                # merge consecutive segments of similar pressure into one polyline
                pts, i = s.pts, 0
                if len(pts) == 1:
                    pts = [pts[0], (pts[0][0] + 0.01, pts[0][1], pts[0][2])]
                while i < len(pts) - 1:
                    bucket = round((pts[i][2] + pts[i + 1][2]) / 2 * 6)
                    pb = Gsk.PathBuilder.new()
                    pb.move_to(pts[i][0], pts[i][1])
                    j = i
                    while j < len(pts) - 1 and round((pts[j][2] + pts[j + 1][2]) / 2 * 6) == bucket:
                        # Catmull-Rom spline through the real points, drawn as a cubic
                        # bezier per segment: smooth curves instead of a jagged polyline
                        p0 = pts[max(j - 1, 0)]
                        p1, p2 = pts[j], pts[j + 1]
                        p3 = pts[min(j + 2, len(pts) - 1)]
                        pb.cubic_to(p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
                                    p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6,
                                    p2[0], p2[1])
                        j += 1
                    stroke = Gsk.Stroke.new(s.width * (0.4 + bucket / 6))
                    stroke.set_line_cap(Gsk.LineCap.ROUND)
                    stroke.set_line_join(Gsk.LineJoin.ROUND)
                    snapshot.append_stroke(pb.to_path(), stroke, rgba)
                    i = j
            for it in self.items:                                  # text, above the pen
                if it.kind == "text":
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue = it.color
                    rgba.alpha = 1.0
                    snapshot.save()
                    snapshot.translate(Graphene.Point().init(it.x, it.y))
                    snapshot.append_layout(self.make_layout(it), rgba)
                    snapshot.restore()
            if not export and self.selected in self.items:         # selection outline + resize handle
                bx, by, bw, bh = self.item_box(self.selected)
                blue = Gdk.RGBA()
                blue.red, blue.green, blue.blue, blue.alpha = 0.1, 0.45, 0.95, 1.0
                pb = Gsk.PathBuilder.new()
                pb.add_rect(Graphene.Rect().init(bx, by, bw, bh))
                snapshot.append_stroke(pb.to_path(), Gsk.Stroke.new(2 / self.zoom), blue)
                hs = 12 / self.zoom
                snapshot.append_color(blue, Graphene.Rect().init(bx + bw - hs / 2, by + bh - hs / 2, hs, hs))

        def content_bounds(self):
            """(x, y, width, height) around everything on the surface, plus a margin."""
            x0 = y0 = math.inf
            x1 = y1 = -math.inf
            for s in self.strokes:
                for px, py, _ in s.pts:
                    x0, y0 = min(x0, px - s.width), min(y0, py - s.width)
                    x1, y1 = max(x1, px + s.width), max(y1, py + s.width)
            for it in self.items:
                bx, by, bw, bh = self.item_box(it)
                x0, y0, x1, y1 = min(x0, bx), min(y0, by), max(x1, bx + bw), max(y1, by + bh)
            if x0 == math.inf:                       # empty surface
                x0, y0, x1, y1 = 0, 0, 400, 300
            return (x0 - EXPORT_MARGIN, y0 - EXPORT_MARGIN,
                    x1 - x0 + 2 * EXPORT_MARGIN, y1 - y0 + 2 * EXPORT_MARGIN)

        def render_png(self, path):
            """Draw everything on the surface, cropped, with GTK's renderer and save it."""
            bx, by, bw, bh = self.content_bounds()
            sc = min(EXPORT_SCALE, EXPORT_MAX_SIDE / max(bw, bh), math.sqrt(EXPORT_MAX_PIXELS / (bw * bh)))
            iw, ih = max(int(bw * sc), 1), max(int(bh * sc), 1)
            snap = Gtk.Snapshot()
            snap.scale(sc, sc)
            snap.translate(Graphene.Point().init(-bx, -by))
            white = Gdk.RGBA()
            white.red = white.green = white.blue = white.alpha = 1.0
            snap.append_color(white, Graphene.Rect().init(bx, by, bw, bh))
            self.draw_content(snap, export=True)
            node = snap.to_node()
            # A private offscreen renderer: the window's own renderer was missing in a long-lived
            # session ("'NoneType' object has no attribute 'render_texture'"), which broke saving.
            if self.export_renderer is None:
                renderer = Gsk.CairoRenderer.new()
                renderer.realize_for_display(Gdk.Display.get_default())
                self.export_renderer = renderer
            texture = self.export_renderer.render_texture(node, Graphene.Rect().init(0, 0, iw, ih))
            if not texture.save_to_png(str(path)):
                raise OSError("could not write the PNG")
            return iw, ih

        # ---- saving and opening ------------------------------------------------
        def has_content(self):
            """Something the user made (the automatic screenshot alone doesn't count)."""
            return bool(self.strokes) or any(not i.auto for i in self.items)

        def write_project(self, path):
            """The editable copy: strokes, pictures, text and the view, in one zip file."""
            items = []
            with zipfile.ZipFile(str(path) + ".tmp", "w", zipfile.ZIP_DEFLATED) as zf:
                for n, it in enumerate(self.items):
                    d = {"kind": it.kind, "x": it.x, "y": it.y, "width": it.width,
                         "text": it.text, "size": it.size, "color": list(it.color), "auto": it.auto}
                    if it.kind == "image":
                        d["image"] = f"images/{n}.png"
                        zf.writestr(d["image"], texture_png(it))
                    items.append(d)
                state = {"version": 1,
                         "view": {"zoom": self.zoom, "ox": self.ox, "oy": self.oy},
                         "strokes": [{"color": list(s.color), "width": s.width,
                                      "pts": [list(p) for p in s.pts]} for s in self.strokes],
                         "items": items}
                zf.writestr("state.json", json.dumps(state))
            os.replace(str(path) + ".tmp", path)

        @staticmethod
        def read_project(path):
            with zipfile.ZipFile(path) as zf:
                state = json.loads(zf.read("state.json"))
                strokes = []
                for sd in state["strokes"]:
                    s = Stroke(tuple(sd["color"]), sd["width"])
                    s.pts = [tuple(p) for p in sd["pts"]]
                    strokes.append(s)
                items = []
                for d in state["items"]:
                    it = Item(d["kind"], d["x"], d["y"], width=d["width"], text=d["text"],
                              size=d["size"], color=tuple(d["color"]), auto=d.get("auto", False))
                    if d["kind"] == "image":
                        data = zf.read(d["image"])
                        it.png = data
                        it.texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                    items.append(it)
            v = state["view"]
            return strokes, items, (v["zoom"], v["ox"], v["oy"])

        def list_notes(self):
            """[(base, mtime, editable)] for the notes made by this app, newest first."""
            notes = []
            if NOTES_DIR.is_dir():
                for md in NOTES_DIR.glob("*.md"):
                    png = md.with_suffix(".png")
                    if not png.exists():
                        continue
                    proj = PROJECT_DIR / f"{md.stem}.pennote"
                    notes.append((md.stem, png.stat().st_mtime, proj.exists()))
            return sorted(notes, key=lambda n: n[1], reverse=True)

        def on_open(self, *_):
            dlg = Gtk.Window(title="Open a note", transient_for=self.win, modal=True,
                             default_width=460, default_height=500)
            notes = self.list_notes()
            if not notes:
                dlg.set_child(Gtk.Label(label="No saved notes yet.", margin_top=24, margin_bottom=24,
                                        margin_start=24, margin_end=24))
            else:
                lb = Gtk.ListBox()
                for base, mtime, editable in notes:
                    when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    row = Gtk.ListBoxRow()
                    text = f"{base}\n{when}" + ("" if editable else "  ·  image only")
                    row.set_child(Gtk.Label(label=text, xalign=0, margin_top=6, margin_bottom=6,
                                            margin_start=10, margin_end=10))
                    row.pennote_base = base
                    lb.append(row)

                def chosen(_lb, row):
                    dlg.close()
                    self.open_note(row.pennote_base)
                lb.connect("row-activated", chosen)
                sc = Gtk.ScrolledWindow()
                sc.set_child(lb)
                dlg.set_child(sc)
            dlg.present()

        def open_note(self, base):
            """Replace the surface with a saved note so it can be continued."""
            if self.dirty and (self.has_content() or self.saved_base):
                self.save()                                  # don't lose what's on screen
            proj = PROJECT_DIR / f"{base}.pennote"
            png = NOTES_DIR / f"{base}.png"
            try:
                if proj.exists():
                    strokes, items, view = self.read_project(proj)
                elif png.exists():                           # saved before notes were editable
                    tex = Gdk.Texture.new_from_filename(str(png))
                    width = min(float(tex.get_width()), 1000.0)
                    strokes, items, view = [], [Item("image", 0.0, 0.0, width=width, texture=tex)], (1.0, 40.0, 40.0)
                else:
                    self.status.set_text(f"Can't find “{base}”.")
                    return False
            except Exception as e:  # noqa: BLE001
                log("open failed:", e)
                self.status.set_text(f"Could not open “{base}”: {e}")
                return False
            self.strokes, self.items, self.selected = strokes, items, None
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.saved_base = base
            self.name_entry.set_text(base)
            self.dirty = False
            self.close_warned = False
            self.set_mode("pen")
            self.set_view(*view)
            self.win.set_title(f"Pen Note: {base}")
            self.status.set_text(f"Opened “{base}”" + ("" if proj.exists() else " (image only: draw on top of it)"))
            return True

        def unique_base(self, wanted):
            base, n = wanted, 2
            while ((NOTES_DIR / f"{base}.md").exists() or (NOTES_DIR / f"{base}.png").exists()):
                if base == self.saved_base:
                    return base
                base = f"{wanted} {n}"
                n += 1
            return base

        def save(self, notify=False):
            if not self.has_content() and self.saved_base is None:
                if notify:
                    self.status.set_text("Nothing drawn yet.")
                return True
            try:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                PROJECT_DIR.mkdir(parents=True, exist_ok=True)
                typed = safe_name(self.name_entry.get_text())
                stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
                if typed:
                    wanted = typed
                elif self.saved_base:
                    wanted = self.saved_base
                else:
                    wanted = f"Pen note {stamp}"
                base = self.unique_base(wanted) if wanted != self.saved_base else wanted
                old = self.saved_base

                self.render_png(NOTES_DIR / f"{base}.png")
                self.write_project(PROJECT_DIR / f"{base}.pennote")

                md = NOTES_DIR / f"{base}.md"
                if old and old != base and (NOTES_DIR / f"{old}.md").exists():
                    # renamed: move the note over, keeping anything the user added to it in Obsidian
                    text = (NOTES_DIR / f"{old}.md").read_text()
                    md.write_text(text.replace(f"![[{old}.png]]", f"![[{base}.png]]"))
                elif not md.exists():                        # never overwrite a note the user may have edited
                    md.write_text(
                        "---\n"
                        f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        "tags: [pen-note]\n"
                        "---\n\n"
                        f"![[{base}.png]]\n")

                if old and old != base:                      # drop the old name's files
                    for f in (NOTES_DIR / f"{old}.md", NOTES_DIR / f"{old}.png",
                              PROJECT_DIR / f"{old}.pennote"):
                        f.unlink(missing_ok=True)
                self.saved_base = base
                self.dirty = False
                self.win.set_title(f"Pen Note: {base}")
                self.status.set_text(f"Saved: Pen Notes/{base}")
                return True
            except Exception as e:  # noqa: BLE001
                log("save failed:", e)
                self.status.set_text(f"Save failed: {e}")
                return False

        def on_close(self, win):
            if self.dirty and (self.has_content() or self.saved_base):
                if not self.save() and not self.close_warned:
                    # Warn once, then let the next close go through: a failing save must never
                    # leave the window impossible to close.
                    self.close_warned = True
                    self.status.set_text(self.status.get_text() + "  (Close again to discard.)")
                    return True
            if screenshot:
                Path(screenshot).unlink(missing_ok=True)
            return False

        # ---- self-test (run with PEN_NOTE_SELFTEST=<mode>; never touches your vault) --------------
        def selftest(self):
            import tempfile
            import cairo
            mode = os.environ["PEN_NOTE_SELFTEST"]
            w, h = self.canvas_size()

            def line(x0, y0, x1, y1):
                self.begin(x0, y0, 0.5, False)
                self.extend((x0 + x1) / 2, (y0 + y1) / 2, 0.6)
                self.extend(x1, y1, 0.5)
                self.finish()

            if mode == "slides":
                deck, pdf = os.environ["PEN_NOTE_TEST_DECK"], os.environ["PEN_NOTE_TEST_PDF"]
                bad = os.environ["PEN_NOTE_TEST_BAD"]
                st = {}

                def pics():
                    return [i for i in self.items if i.kind == "image"]

                def after_bad():
                    log("selftest slides, bad file: handled without crashing:",
                        "Could not open" in self.status.get_text() and not self.items)
                    self.add_slide_file(deck, done=after_deck)

                def after_deck():
                    imgs = pics()
                    hs = [i.width * i.texture.get_height() / i.texture.get_width() for i in imgs]
                    ordered = all(imgs[k + 1].y > imgs[k].y for k in range(len(imgs) - 1))
                    gaps = all(abs(imgs[k + 1].y - (imgs[k].y + hs[k]) - SLIDE_GAP_RATIO * hs[k]) < 0.5
                               for k in range(len(imgs) - 1))
                    sx, sy = imgs[0].x * self.zoom + self.ox, imgs[0].y * self.zoom + self.oy
                    log("selftest slides, deck: slides:", len(imgs), "| in order:", ordered,
                        "| blank gap after each:", gaps, "| one column:", len({round(i.x, 3) for i in imgs}) == 1,
                        "| first slide at top-left of view:", abs(sx - 40) < 1 and abs(sy - 40) < 1,
                        "| picture size:", (imgs[0].texture.get_width(), imgs[0].texture.get_height()))
                    st["n"] = len(imgs)
                    st["right"] = max(i.x + i.width for i in imgs)
                    st["y1"] = imgs[0].y + hs[0] + 60
                    self.add_slide_file(pdf, done=after_pdf)

                def after_pdf():
                    imgs = pics()
                    new = imgs[st["n"]:]
                    log("selftest slides, pdf: added:", len(new),
                        "| placed to the right of the first deck:", all(i.x > st["right"] for i in new))
                    self.on_undo()
                    undo_ok = len(pics()) == st["n"]
                    self.on_redo()
                    log("selftest slides: one undo removes the whole second deck:", undo_ok,
                        "| redo brings it back:", len(pics()) == st["n"] + len(new))
                    # write a note in the gap between slide 1 and 2
                    p0 = (pics()[0].x + 100) * self.zoom + self.ox
                    p1 = st["y1"] * self.zoom + self.oy
                    line(p0, p1, p0 + 200, p1 + 20)
                    self.name_entry.set_text("slides demo")
                    ok = self.save()
                    base = self.saved_base
                    dims = struct.unpack(">II", (NOTES_DIR / f"{base}.png").read_bytes()[16:24])
                    log("selftest slides save ok:", ok, "| png", dims,
                        "| within limits:", max(dims) <= EXPORT_MAX_SIDE and dims[0] * dims[1] <= EXPORT_MAX_PIXELS * 1.01,
                        "| editable copy kb:", (PROJECT_DIR / f"{base}.pennote").stat().st_size // 1024)
                    n_items, n_strokes = len(self.items), len(self.strokes)
                    self.strokes, self.items = [], []
                    self.open_note(base)
                    log("selftest slides reopen: slides and note restored:",
                        len(self.items) == n_items and len(self.strokes) == n_strokes)
                    left = list(WORK_DIR.glob("job-*"))
                    log("selftest slides: work folder cleaned up:", not left)
                    self.win.close()

                self.add_slide_file(bad, done=after_bad)
                return False
            if mode == "slides3":
                import cairo
                d = tempfile.mkdtemp()
                pdfp = f"{d}/many.pdf"
                surf = cairo.PDFSurface(pdfp, 595, 842)
                c = cairo.Context(surf)
                for n in range(1, 31):
                    c.set_source_rgb(0.9, 0.95, 1)
                    c.paint()
                    c.set_source_rgb(0, 0, 0.4)
                    c.set_font_size(160)
                    c.move_to(180, 480)
                    c.show_text(str(n))
                    surf.show_page()
                surf.finish()
                st = {"t0": time.monotonic(), "first": None, "end": None}

                def pics():
                    return [i for i in self.items if i.kind == "image"]

                def poll():
                    shown = sum(len(v) for v in self.pending.values())
                    if shown and st["first"] is None:
                        st["first"] = time.monotonic() - st["t0"]
                        st["shown_then"] = shown
                        line(100, 100, 200, 160)          # draw while the rest of the deck is still loading
                    return st["end"] is None

                GLib.timeout_add(20, poll)

                def after_first():
                    st["end"] = time.monotonic() - st["t0"]
                    n, strokes = len(pics()), len(self.strokes)
                    log("selftest slides3, 30-page pdf: slides:", n,
                        "| first slide visible after %.1fs, whole deck after %.1fs" % (st["first"], st["end"]),
                        "| first slide shown before the deck was complete:", st["first"] < st["end"],
                        "| pending when first shown:", st["shown_then"],
                        "| nothing left pending:", not self.pending)
                    self.on_undo()
                    log("selftest slides3 undo: one undo removes the whole deck and keeps my stroke:",
                        len(pics()) == 0 and len(self.strokes) == strokes == 1)
                    self.on_redo()
                    t2 = time.monotonic()

                    def after_second():
                        took = time.monotonic() - t2
                        log("selftest slides3 cache: second add of the same file: slides now:", len(pics()),
                            "| took %.1fs" % took, "| served from cache:", any("cache" in m for m in self.msgs),
                            "| cache dir:", sorted(x.name[-12:] for x in CACHE_DIR.iterdir()))
                        shutil.rmtree(d, ignore_errors=True)
                        self.win.close()
                    self.add_slide_file(pdfp, done=after_second)

                self.add_slide_file(pdfp, done=after_first)
                return False
            if mode == "slides2":
                # two decks started at once plus a duplicate: no collision, no silence
                deck, deck2 = os.environ["PEN_NOTE_TEST_DECK"], os.environ["PEN_NOTE_TEST_DECK2"]
                st = {"left": 2}

                def finished():
                    st["left"] -= 1
                    if st["left"]:
                        return
                    imgs = [i for i in self.items if i.kind == "image"]
                    log("selftest slides2: both decks arrived (6 slides):", len(imgs) == 6,
                        "| duplicate refused at once:", st["dup"],
                        "| LibreOffice progress shown:", any("Converting" in m for m in self.msgs),
                        "| per-slide progress shown:", any(" ready" in m and "slide 3 of 3" in m for m in self.msgs),
                        "| waited its turn (informational):", any("Waiting" in m for m in self.msgs),
                        "| spinner hidden again:", not self.spinner.get_visible(),
                        "| work folder clean:", not list(WORK_DIR.glob("job-*")))
                    self.win.close()

                self.add_slide_file(deck, done=finished)
                self.add_slide_file(deck2, done=finished)
                self.add_slide_file(deck)                 # duplicate of the first: refused
                st["dup"] = "already being opened" in self.status.get_text()
                return False
            if mode == "shot":
                it = self.items[0]
                bx, by, bw, bh = self.item_box(it)
                log("selftest shot: placed:", it.auto, "| right edge at %.0f%% of view width,"
                    " bottom at %.0f%% of view height" % (100 * (bx + bw) / w, 100 * (by + bh) / h))
                self.win.close()
                log("selftest shot: auto screenshot alone is not saved:",
                    not any(NOTES_DIR.glob("*")) if NOTES_DIR.exists() else True)
                return False
            if mode == "closefail":
                def boom(*a, **k):
                    raise OSError("simulated save failure")
                self.render_png = boom
                line(100, 100, 300, 200)
                self.win.close()
                first = self.win.get_visible()
                self.win.close()
                log("selftest closefail: stays open after 1st close:", first,
                    "| closed after 2nd:", not self.win.get_visible())
                return False
            if mode == "close":
                line(100, 100, 300, 200)
                self.win.close()
                log("selftest close: window closed:", not self.win.get_visible(),
                    "| saved_base:", self.saved_base)
                return False

            # --- full test ---
            line(100, 100, 400, 220)                                  # a stroke near the origin
            self.set_view(1.0, -6000, -4500)                          # pan far away: no limits
            far_ok = self.ox == -6000 and self.oy == -4500
            line(300, 300, 500, 380)                                  # a stroke ~6000 px away
            wx, wy = self.strokes[-1].pts[0][:2]
            log("selftest boundless: pan unlimited:", far_ok, "| far stroke at world", (round(wx), round(wy)),
                "ok:", abs(wx - 6300) < 1 and abs(wy - 4800) < 1)
            self.set_view(1.0, 0, 0)

            self.zoom_by(2.0, w * 0.75, h * 0.5)                      # zoom must keep points where drawn
            px, py = w * 0.9, h * 0.6
            cx, cy = self.to_world(px, py)
            back = (cx * self.zoom + self.ox, cy * self.zoom + self.oy)
            log("selftest zoom", round(self.zoom, 2), "roundtrip ok:",
                abs(back[0] - px) < 0.01 and abs(back[1] - py) < 0.01)
            self.set_view(1.0, 0, 0)

            before = len(self.strokes)                                # eraser undo / redo
            self.begin(250, 160, 0.5, True)
            self.extend(255, 162, 0.5)
            self.finish()
            erased = len(self.strokes)
            self.on_undo()
            undo_ok = erased < before and len(self.strokes) == before
            self.on_redo()
            redo_ok = len(self.strokes) == erased
            self.on_undo()
            log("selftest eraser: strokes", before, "->", erased, "| undo ok:", undo_ok, "| redo ok:", redo_ok)

            d = tempfile.mkdtemp()                                    # pictures and text
            try:
                s = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 200)
                c = cairo.Context(s)
                c.set_source_rgb(0.95, 0.55, 0.1)
                c.paint()
                c.set_source_rgb(1, 1, 1)
                c.arc(150, 100, 60, 0, 6.3)
                c.fill()
                s.write_to_png(f"{d}/t.png")
                tex = Gdk.Texture.new_from_filename(f"{d}/t.png")
            finally:
                shutil.rmtree(d, ignore_errors=True)
            self.add_image(tex, w * 0.7, h * 0.3)
            log("selftest picture keeps the pen drawing:", self.mode == "pen")
            self.set_mode("move")
            self.commit_text(None, self.to_world(w * 0.1, h * 0.75), "Hello\nhandwritten notes")
            img = [i for i in self.items if i.kind == "image"][0]
            bx, by, bw, bh = self.item_box(img)
            x0 = img.x
            self.begin(bx + bw / 2, by + bh / 2, 0.5, False)
            self.extend(bx + bw / 2 - 150, by + bh / 2 + 40, 0.5)
            self.finish()
            moved = img.x < x0
            self.on_undo()
            img = [i for i in self.items if i.kind == "image"][0]
            log("selftest picture: items", len(self.items), "moved ok:", moved,
                "| undo restores position:", abs(img.x - x0) < 1e-9)
            self.on_redo()
            self.set_mode("pen")

            # save: PNG cropped to the content (far stroke included), md, hidden editable copy
            self.name_entry.set_text("selftest / name")
            ok = self.save(notify=True)
            base = self.saved_base
            png = NOTES_DIR / f"{base}.png"
            dims = struct.unpack(">II", png.read_bytes()[16:24])
            log("selftest save ok:", ok, "| base:", base, "| png", dims,
                "| md:", (NOTES_DIR / f"{base}.md").exists(),
                "| editable copy:", (PROJECT_DIR / f"{base}.pennote").exists())
            log("selftest png cropped and bounded:", max(dims) <= EXPORT_MAX_SIDE
                and dims[0] * dims[1] <= EXPORT_MAX_PIXELS * 1.01 and max(dims) > 1000)

            # reopen: everything comes back and can be continued
            n_strokes, n_items = len(self.strokes), len(self.items)
            pts0 = [list(p) for p in self.strokes[0].pts]
            tex_dims = [(i.texture.get_width(), i.texture.get_height()) for i in self.items if i.kind == "image"]
            self.strokes, self.items = [], []
            self.set_view(1.0, 0, 0)
            opened = self.open_note(base)
            same = (len(self.strokes) == n_strokes and len(self.items) == n_items
                    and [list(p) for p in self.strokes[0].pts] == pts0
                    and [(i.texture.get_width(), i.texture.get_height())
                         for i in self.items if i.kind == "image"] == tex_dims
                    and [i.text for i in self.items if i.kind == "text"] == ["Hello\nhandwritten notes"])
            line(100, 500, 200, 520)                                  # keep drawing on the reopened note
            self.on_undo()
            log("selftest reopen: opened:", opened, "| identical:", same,
                "| can draw and undo:", len(self.strokes) == n_strokes)

            # a note saved before notes were editable opens as a picture to draw on
            legacy = NOTES_DIR / "legacy note"
            png.replace(NOTES_DIR / "legacy note.png")
            legacy.with_suffix(".md").write_text("My own text\n![[legacy note.png]]\n")
            for f in PROJECT_DIR.glob("*.pennote"):
                f.unlink()
            self.strokes, self.items = [], []
            self.saved_base = None
            opened = self.open_note("legacy note")
            ok_legacy = (opened and len(self.items) == 1 and self.items[0].kind == "image"
                         and not self.strokes)
            line(100, 100, 200, 160)
            self.save()
            md_text = legacy.with_suffix(".md").read_text()
            log("selftest legacy open: ok:", ok_legacy, "| save keeps my markdown untouched:",
                md_text == "My own text\n![[legacy note.png]]\n",
                "| now editable:", (PROJECT_DIR / "legacy note.pennote").exists())

            # rename keeps what was added to the note in Obsidian and fixes the embed
            legacy.with_suffix(".md").write_text("My own text\n![[legacy note.png]]\n")
            self.name_entry.set_text("renamed note")
            line(100, 200, 200, 260)
            self.save()
            ren = (NOTES_DIR / "renamed note.md").read_text()
            log("selftest rename: ok:", ren == "My own text\n![[renamed note.png]]\n",
                "| old files removed:", not (NOTES_DIR / "legacy note.md").exists()
                and not (NOTES_DIR / "legacy note.png").exists()
                and not (PROJECT_DIR / "legacy note.pennote").exists())
            self.win.close()
            return False

    app = App()
    return app.run([sys.argv[0]])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("daemon")
    c = sub.add_parser("canvas")
    c.add_argument("--screenshot")
    c.add_argument("--open", dest="open_name")
    c.add_argument("--slides", help="a PowerPoint/OpenDocument/PDF file to lay out on the canvas")
    args = ap.parse_args()
    if args.mode == "daemon":
        run_daemon()
    else:
        sys.exit(run_canvas(args.screenshot, args.open_name, args.slides))


if __name__ == "__main__":
    main()
