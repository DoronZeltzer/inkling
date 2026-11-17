#!/usr/bin/env python3
"""Pen Note: double-tap the pen on the screen to open a boundless canvas.

  pen_note.py daemon                 watch the pen, open the canvas on a double tap
  pen_note.py canvas [--screenshot FILE] [--open NAME] [FILE ...]
                                     open the canvas directly (also used by the daemon and
                                     "Open with Pen Note": .pennote notes, pictures, slides)

Each save writes, into the Obsidian vault: a PNG of everything on the canvas, a markdown
note that embeds it (with an "Edit in Pen Note" link), and a hidden editable copy so the note
can be opened and continued. "Save PDF" keeps opened PDF pages sharp and puts your marks on top.

Needs: python3-gi with GTK4, poppler-utils (pdftoppm, pdfinfo), python3-pypdf, python3-cairo.
"""
import argparse
import fcntl
import hashlib
import html
import io
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
import unicodedata
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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
HIGHLIGHT = (1.0, 0.88, 0.1)
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
MAX_SLIDES = 2000
# The snap-packaged LibreOffice can only read non-hidden folders inside your home folder,
# so decks are copied into this work folder (and removed again) for conversion.
WORK_DIR = SELF.parent / "work"
EXPORT_MAX_PAGES = 40       # PDF pages drawn into the saved PNG preview (the rest show as grey boxes)
PAGE_TEXTURES = 24          # PDF pages kept drawn in memory; the others are drawn again when you scroll back
# The PDF behind every page (an opened PDF, a converted deck, a turned copy). A saved note carries its
# own copy and brings it back when opened, so copies not used for a while are cleared away.
PDF_STORE = WORK_DIR / "pdf-src"


def seg_dist(px, py, ax, ay, bx, by):
    """Distance from point (px, py) to the line segment (ax, ay)-(bx, by)."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def stroke_centre(s):
    xs, ys = [p[0] for p in s.pts], [p[1] for p in s.pts]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def stroke_segments(s):
    """[(line width, [(p1, c1, c2, p2), ...])]: runs of similar pressure, each one a smooth curve.
    A Catmull-Rom spline through the pen points, as one cubic bezier per segment."""
    pts = s.pts
    if len(pts) == 1:
        pts = [pts[0], (pts[0][0] + 0.01, pts[0][1], pts[0][2])]
    n = len(pts)
    # average each point's pressure with its neighbours, so the width changes gradually
    pr = [(pts[max(i - 1, 0)][2] + pts[i][2] + pts[min(i + 1, n - 1)][2]) / 3 for i in range(n)]

    def level(i):
        return round((pr[i] + pr[i + 1]) / 2 * 12)

    runs, i = [], 0
    while i < n - 1:
        lv, segs, j = level(i), [], i
        while j < n - 1 and level(j) == lv:
            p0, p1, p2 = pts[max(j - 1, 0)], pts[j], pts[j + 1]
            p3 = pts[min(j + 2, n - 1)]
            segs.append(((p1[0], p1[1]),
                         (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6),
                         (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6),
                         (p2[0], p2[1])))
            j += 1
        runs.append((s.width * (0.4 + lv / 12), segs))
        i = j
    return runs


def parse_pages(text, total):
    """'1-3,5' -> [1, 2, 3, 5]. Raises ValueError with a message meant for the user."""
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b or a)
        except ValueError:
            raise ValueError(f"“{part}” isn't a page number or range") from None
        if not 1 <= lo <= hi <= total:
            raise ValueError(f"Pages go from 1 to {total}")
        out.extend(range(lo, hi + 1))
    if not out:
        raise ValueError("Type the pages you want, for example 1-3,5")
    return out


BIDI_MARKS = set("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def is_rtl(text):
    """True if the first letter is from a right-to-left script (Hebrew, Arabic)."""
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return True
        if kind == "L":
            return False
    return False


def pdf_text_lines(pdf, n):
    """(page width, page height, [(text, x0, y0, x1, y1)]) for page n (1-based), in points from the
    top-left of the crop box. No lines for a scanned page (it has no text to pick)."""
    def run(*args):
        return subprocess.run(["pdftotext", *args, "-cropbox", "-f", str(n), "-l", str(n), str(pdf), "-"],
                              capture_output=True, text=True, timeout=60).stdout
    out = run("-bbox-layout")
    size = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', out)
    if not size:
        return 0.0, 0.0, []
    # Right-to-left text comes out of the box mode with every word's letters reversed. The plain mode
    # gives it in reading order, so those lines are found again by their letters and used instead.
    plain = [" ".join("".join(ch for ch in ln if ch not in BIDI_MARKS).split()) for ln in run().splitlines()]
    pool = [(ln, sorted(c for c in ln if not c.isspace())) for ln in plain if ln]
    lines = []
    for m in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>',
                         out, re.S):
        words = re.findall(r"<word[^>]*>(.*?)</word>", m.group(5), re.S)
        text = html.unescape(" ".join(words)).strip()
        if not text:
            continue
        if any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in text):
            letters = sorted(c for c in text if not c.isspace())
            for k, (ln, ln_letters) in enumerate(pool):
                if ln_letters == letters:
                    text = ln
                    del pool[k]
                    break
        lines.append((text, *map(float, m.groups()[:4])))
    return float(size.group(1)), float(size.group(2)), lines


def page_ratio(page):
    """Height over width of a PDF page as a viewer shows it (crop box, turned)."""
    box = page.cropbox
    w, h = float(box.width), float(box.height)
    if page.rotation % 180:
        w, h = h, w
    return h / w


def migrate_page(it, old_ratio=None):
    """Set a page's proportions from its PDF. A note saved while page pictures still showed the whole
    media box (old_ratio = that picture's height/width) is lined up with the crop box instead."""
    from pypdf import PdfReader
    page = PdfReader(it.pdf[0]).pages[it.pdf[1]]
    it.ratio = page_ratio(page)
    if old_ratio is None or abs(it.ratio - old_ratio) < 0.002 or page.rotation % 360:
        return
    media, crop = page.mediabox, page.cropbox
    k = it.width / float(media.width)
    it.x += float(crop.left - media.left) * k
    it.y += float(media.top - crop.top) * k
    it.width = float(crop.width) * k


def strip_text_in_rects(page, rects):
    """Delete from a page the text drawn inside the rectangles (x0, y0, x1, y1 in the page's own units,
    y up): the text-drawing operations whose starting point is inside one. Returns how many were removed.
    Text kept inside embedded form objects is left alone."""
    from pypdf.generic import ArrayObject, ByteStringObject
    content = page.get_contents()
    if content is None:
        return 0
    ident = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def mul(m, n):
        return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3], m[2] * n[0] + m[3] * n[2],
                m[2] * n[1] + m[3] * n[3], m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5])

    def inside(x, y):
        return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects)

    ctm, stack, tm, tlm, lead, removed = ident, [], ident, ident, 0.0, 0
    for operands, op in content.operations:
        try:
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else ident
            elif op == b"cm":
                ctm = mul(tuple(float(v) for v in operands), ctm)
            elif op == b"BT":
                tm = tlm = ident
            elif op == b"Tm":
                tm = tlm = tuple(float(v) for v in operands)
            elif op in (b"Td", b"TD"):
                if op == b"TD":
                    lead = -float(operands[1])
                tlm = mul((1.0, 0.0, 0.0, 1.0, float(operands[0]), float(operands[1])), tlm)
                tm = tlm
            elif op == b"TL":
                lead = float(operands[0])
            elif op in (b"T*", b"'", b'"'):
                tlm = mul((1.0, 0.0, 0.0, 1.0, 0.0, -lead), tlm)
                tm = tlm
            if op in (b"Tj", b"TJ", b"'", b'"'):
                x, y = mul(tm, ctm)[4:]
                if inside(x, y):
                    operands[-1] = ArrayObject() if op == b"TJ" else ByteStringObject(b"")
                    removed += 1
        except (ValueError, IndexError, TypeError):
            continue
    if removed:
        page.replace_contents(content)
    return removed


def prune_pdf_store(days=14):
    cutoff = time.time() - days * 86400
    try:
        for f in PDF_STORE.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def render_pdf_page(pdf, n):
    """PNG bytes of page n (1-based) of a PDF, SLIDE_RENDER_WIDTH pixels wide."""
    with tempfile.TemporaryDirectory(prefix="page-") as d:
        out = Path(d) / "p"
        r = subprocess.run(["pdftoppm", "-png", "-cropbox", "-singlefile", "-f", str(n), "-l", str(n),
                            "-scale-to-x", str(SLIDE_RENDER_WIDTH), "-scale-to-y", "-1",
                            str(pdf), str(out)], capture_output=True, text=True, timeout=120)
        png = Path(str(out) + ".png")
        if not png.exists():
            raise RuntimeError(f"could not render page {n}: {r.stderr.strip()[-120:]}")
        return png.read_bytes()


_pdf_hashes = {}


def pdf_member(path):
    """Name of a PDF inside a saved note: pdfs/<content hash>.pdf (the hash is worked out once)."""
    path = str(path)
    if path not in _pdf_hashes:
        _pdf_hashes[path] = file_sha1(path)
    return f"pdfs/{_pdf_hashes[path]}.pdf"


def ensure_edit_link(md, base):
    """Make sure a note carries a link that opens it, editable, in Pen Note (works from Obsidian)."""
    link = f"[Edit in Pen Note](pennote://open/{quote(base, safe='')})"
    text = md.read_text()
    if "pennote://open/" in text:
        new = re.sub(r"\[Edit in Pen Note\]\(pennote://open/[^)]*\)", lambda _: link, text)
    else:
        new = text.rstrip("\n") + f"\n\n{link}\n"
    if new != text:
        md.write_text(new)


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


def convert_slides(path, progress=lambda text: None, on_page=None):
    """Turn a PowerPoint/OpenDocument/PDF file into PDF pages that can be laid out.

    A PDF is used as it is; PowerPoint is slow (LibreOffice needs most of a minute for a big deck), so
    call this off the UI thread. `progress(text)` reports what it is doing and
    `on_page(number, total, ratio, pdf_path)` is called for each page, in order (ratio = height / width).
    The pages themselves are drawn later, as they come into view. A file that was converted before
    comes straight from the store. Returns the number of pages. Raises RuntimeError."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SLIDE_EXTS:
        raise RuntimeError("that isn't a PowerPoint, OpenDocument or PDF file")
    pdf_store = PDF_STORE / f"{file_sha1(path)}.pdf"
    if _convert_lock.locked():
        progress("Waiting for the other deck to finish…")
    with _convert_lock:
        if pdf_store.exists():                           # converted before: instant
            progress("Found this deck in the cache")
            os.utime(pdf_store)
        else:
            WORK_DIR.mkdir(exist_ok=True)
            PDF_STORE.mkdir(parents=True, exist_ok=True)
            job = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_DIR))
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
                tmp = pdf_store.with_suffix(".tmp")
                shutil.copyfile(pdf, tmp)
                os.replace(tmp, pdf_store)
            except subprocess.TimeoutExpired:
                raise RuntimeError("conversion took too long (over 5 minutes)") from None
            finally:
                shutil.rmtree(job, ignore_errors=True)
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_store))
            if reader.is_encrypted and not reader.decrypt(""):
                raise RuntimeError("this PDF is password protected")
            total = min(len(reader.pages), MAX_SLIDES)
            ratios = [page_ratio(reader.pages[i]) for i in range(total)]
        except RuntimeError:
            pdf_store.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001
            pdf_store.unlink(missing_ok=True)
            raise RuntimeError(f"no pages found: {str(e)[-120:] or 'empty file?'}") from None
        if total == 0:
            pdf_store.unlink(missing_ok=True)
            raise RuntimeError("no pages found: empty file?")
        progress(f"Opening {total} page{'s' if total != 1 else ''}…")
        for i, ratio in enumerate(ratios, 1):
            if on_page:
                on_page(i, total, ratio, str(pdf_store))
        return total


class Item:
    """A picture or a text box. x, y, image width and text size are in world pixels
    (1 world pixel = 1 screen pixel at zoom 1)."""
    __slots__ = ("kind", "x", "y", "width", "texture", "text", "size", "color", "png", "auto", "pdf", "sign", "ratio")

    def __init__(self, kind, x, y, width=400.0, texture=None, text="", size=24.0,
                 color=(0.08, 0.08, 0.08), png=None, auto=False, pdf=None, sign=None, ratio=None):
        self.kind, self.x, self.y = kind, x, y
        self.width, self.texture, self.text, self.size, self.color = width, texture, text, size, color
        self.png = png        # cached PNG bytes of the picture (for saving)
        self.auto = auto      # the screenshot placed automatically: doesn't count as "content"
        self.pdf = pdf        # (path of the PDF, page index) when this picture is a PDF page
        self.ratio = ratio    # height / width of a PDF page (it has no picture until it is drawn)
        self.sign = sign      # a signature: {"aspect": h/w, "strokes": [{"width", "pts": [[u, v, p]]}]},
                              # points as fractions of the signature's width

    def clone(self):
        return Item(self.kind, self.x, self.y, self.width, self.texture, self.text, self.size,
                    self.color, self.png, self.auto, self.pdf, self.sign, self.ratio)


def run_canvas(screenshot, open_name=None, slides=None, files=()):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Pango", "1.0")
    from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango
    try:                       # needs python3-gi-cairo: typed text then goes into PDFs as real text
        gi.require_version("PangoCairo", "1.0")
        import cairo
        from gi.repository import PangoCairo
        PangoCairo.create_layout(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)))
    except Exception:  # noqa: BLE001
        PangoCairo = None

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
        __slots__ = ("color", "width", "pts", "cache", "alpha")

        def __init__(self, color, width, alpha=1.0):
            self.color, self.width, self.alpha, self.pts = color, width, alpha, []  # pts: (x, y, pressure)
            self.cache = None   # (point count, [(path, Gsk.Stroke)]): built once per stroke, not per frame

    def sign_strokes(it):
        """A signature item as ordinary strokes in world pixels."""
        out = []
        for sd in it.sign["strokes"]:
            st = Stroke(it.color, sd["width"] * it.width)
            st.pts = [(it.x + u * it.width, it.y + v * it.width, p) for u, v, p in sd["pts"]]
            out.append(st)
        return out

    def stroke_runs(s):
        runs = []
        for width, segs in stroke_segments(s):
            pb = Gsk.PathBuilder.new()
            pb.move_to(*segs[0][0])
            for _, c1, c2, p2 in segs:
                pb.cubic_to(*c1, *c2, *p2)
            stroke = Gsk.Stroke.new(width)
            stroke.set_line_cap(Gsk.LineCap.ROUND)
            stroke.set_line_join(Gsk.LineJoin.ROUND)
            runs.append((pb.to_path(), stroke))
        return runs

    def texture_png(item):
        if item.png is None:
            item.png = item.texture.save_to_png_bytes().get_data()
        return item.png

    class App(Gtk.Application):
        def __init__(self):
            suffix = ".selftest" if os.environ.get("PEN_NOTE_SELFTEST") else ""
            super().__init__(application_id="local.pennote.canvas" + suffix,
                             flags=Gio.ApplicationFlags.HANDLES_OPEN)   # files from "Open with"
            self.win = None
            self.signing = None       # while drawing a new signature: what the surface held before
            self.text_cover = None    # set while retyping a line of a PDF's own text
            self.line_cache = {}      # (pdf, page) -> its text lines
            self.tex_cache = OrderedDict()   # (pdf, page) -> picture of that page, newest last
            self.tex_pending = set()         # pages being drawn right now
            self.tex_failed = set()
            self.export_pages = set()
            self.render_pool = ThreadPoolExecutor(max_workers=3)
            self.erase_snapped = False
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
        def do_open(self, files, n_files, hint):
            """Files chosen with "Open with Pen Note" (also reaches an already open canvas)."""
            self.do_activate()
            for f in files:
                uri = f.get_uri() or ""
                if uri.startswith("pennote:"):               # the "Edit in Pen Note" link in Obsidian
                    self.open_note(safe_name(Path(unquote(urlparse(uri).path)).name))
                elif f.get_path():
                    self.open_path(f.get_path())

        def open_path(self, p):
            ext = Path(p).suffix.lower()
            stem = Path(p).stem
            if (ext in (".png", ".md") and Path(p).resolve().parent == NOTES_DIR.resolve()
                    and (PROJECT_DIR / f"{stem}.pennote").exists()):
                self.open_note(stem)                         # a Pen Note's own picture: edit the note
            elif ext == ".pennote":
                self.open_project_file(p)
            elif ext in SLIDE_EXTS:
                self.add_slide_file(p)
            else:
                try:
                    self.add_image(Gdk.Texture.new_from_filename(p))
                except GLib.Error:
                    self.status.set_text(f"Can't open “{Path(p).name}”: not a note, picture or slide file.")

        def do_activate(self):
            if self.win is not None:                 # already open: just bring it forward
                self.win.present()
                return
            win = Gtk.ApplicationWindow(application=self, title="Pen Note")
            self.win = win
            win.set_default_size(1100, 760)

            outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            win.set_child(outer)
            outer.append(self.build_sidebar())
            outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            outer.append(root)

            def new_bar():
                # in a scroller, so a long row of tools never makes the window wider than the screen
                bar = Gtk.Box(spacing=6, margin_top=4, margin_bottom=2, margin_start=8, margin_end=8)
                scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
                scroller.set_child(bar)
                root.append(scroller)
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
            add_button(bar, "−", lambda *_: self.zoom_by(1 / 1.4), "Zoom out")
            add_button(bar, "+", lambda *_: self.zoom_by(1.4), "Zoom in")
            add_button(bar, "Fit", lambda *_: self.fit_all(), "Show everything on the surface")
            add_button(bar, "Undo", self.on_undo, "Undo (Ctrl+Z)")
            add_button(bar, "Redo", self.on_redo, "Redo (Ctrl+Shift+Z)")
            add_button(bar, "Clear", self.on_clear, "Remove everything")
            add_button(bar, "Save PDF", self.on_save_pdf,
                       "Save as a PDF: opened PDF pages stay sharp, your marks go on top")
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
            for name, label, tip in (("highlight", "Highlight", "Yellow highlighter"),
                                     ("edittext", "Edit text", "Change text that is already in a PDF page"),
                                     ("eraser", "Eraser", "Erase pen strokes"),
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

            tsize = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 8, 72, 1)
            tsize.set_value(self.text_px)
            tsize.set_draw_value(False)
            tsize.set_size_request(90, -1)
            tsize.set_tooltip_text("Text size (for new text, and for the text you have selected)")
            tsize.connect("value-changed", self.on_text_size)
            bar.append(tsize)
            add_button(bar, "Delete", self.on_delete_selected, "Delete the selected picture or text")
            add_button(bar, "Paste", self.on_paste, "Paste a picture from the clipboard (Ctrl+V)")
            tip = Gtk.Label(label="Drag with a finger to move around · drop pictures or slides here", hexpand=True, xalign=1,
                            ellipsize=Pango.EllipsizeMode.END)
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
            prune_pdf_store()
            self.refresh_recent()
            self.area.grab_focus()
            if slides:
                self.add_slide_file(slides)
            if os.environ.get("PEN_NOTE_SELFTEST"):
                GLib.timeout_add(1500, lambda: __import__("selftest").run(self))

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
                "edittext": "Tool: Edit text (click a line of text in a PDF, retype it, click away to finish)",
                "highlight": "Tool: Highlight (yellow, for marking text)",
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
                if self.mode in ("eraser", "move", "highlight", "edittext"):  # picking a color means "draw"
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
            elif keyval == Gdk.KEY_F11:                      # full screen on / off
                self.win.unfullscreen() if self.win.is_fullscreen() else self.win.fullscreen()
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
        # ---- left panel: what to open, page tools, recent notes ------------------
        def build_sidebar(self):
            side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                           margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            side.set_size_request(168, -1)

            def head(text):
                lab = Gtk.Label(label=text, xalign=0, margin_top=10, margin_bottom=2)
                lab.add_css_class("dim-label")
                lab.add_css_class("caption-heading")
                side.append(lab)

            def button(label, cb, tip=None):
                b = Gtk.Button(label=label)
                b.add_css_class("flat")
                b.get_child().set_halign(Gtk.Align.START)
                b.connect("clicked", cb)
                if tip:
                    b.set_tooltip_text(tip)
                side.append(b)

            head("OPEN")
            button("Note…", self.on_open, "A note you saved before, to continue it")
            button("PDF…", lambda *_: self.pick_file("Open a PDF", "PDF", [".pdf"], self.add_slide_file),
                   "Open a PDF: write on it, highlight, then Save PDF. Open a second one to join them")
            button("Picture…", lambda *_: self.pick_file(
                "Add a picture", "Pictures", [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"], self.add_picture_file))
            button("Slides…", lambda *_: self.pick_file(
                "Add slides", "PowerPoint, OpenDocument", sorted(SLIDE_EXTS - {".pdf"}), self.add_slide_file),
                "A PowerPoint or OpenDocument presentation: every slide becomes a PDF page")
            head("PDF PAGES")
            button("Rotate page", self.rotate_page, "Turn the selected page (or the one in the middle of the view)")
            button("Delete page", self.delete_page)
            button("Move page up", lambda *_: self.move_page(-1))
            button("Move page down", lambda *_: self.move_page(1))
            button("Split…", self.on_split, "Save only some pages as a new PDF")
            head("SIGN")
            button("Sign", self.on_sign, "Place your signature on the page (you draw it the first time)")
            button("New signature…", self.start_signature, "Draw your signature again")
            self.sign_row = Gtk.Box(spacing=6, visible=False)
            for label, cb in (("Done", self.finish_signature), ("Cancel", self.cancel_signature)):
                b = Gtk.Button(label=label)
                b.connect("clicked", cb)
                self.sign_row.append(b)
            self.sign_row.get_first_child().add_css_class("suggested-action")
            side.append(self.sign_row)
            head("RECENT")
            self.recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            side.append(self.recent_box)
            sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sc.set_child(side)
            return sc

        def refresh_recent(self):
            box = getattr(self, "recent_box", None)
            if box is None:
                return
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            for base, _mtime, _editable in self.list_notes()[:6]:
                b = Gtk.Button(label=base)
                b.add_css_class("flat")
                lab = b.get_child()
                lab.set_halign(Gtk.Align.START)
                lab.set_ellipsize(Pango.EllipsizeMode.END)
                lab.set_max_width_chars(20)
                b.set_tooltip_text(base)
                b.connect("clicked", lambda _b, name=base: self.open_note(name))
                box.append(b)

        def pick_file(self, title, kind, suffixes, callback):
            dlg = Gtk.FileDialog(title=title)
            flt = Gtk.FileFilter()
            flt.set_name(kind)
            for ext in suffixes:
                flt.add_suffix(ext[1:])
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(flt)
            dlg.set_filters(store)
            dlg.set_default_filter(flt)

            def done(d, res):
                try:
                    f = d.open_finish(res)
                except GLib.Error:
                    return                                   # cancelled
                if f and f.get_path():
                    callback(f.get_path())
            dlg.open(self.win, None, done)

        def add_picture_file(self, path):
            try:
                self.add_image(Gdk.Texture.new_from_filename(path))
            except GLib.Error:
                self.status.set_text(f"Can't open “{Path(path).name}” as a picture.")

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
                        lambda i, total, ratio, pdf: GLib.idle_add(self.slide_ready, path, name, i, total, ratio, pdf))
                    err = None
                except Exception as e:  # noqa: BLE001
                    n, err = 0, str(e)
                GLib.idle_add(self.slides_done, path, name, n, err, done)
            threading.Thread(target=work, daemon=True).start()

        def slide_ready(self, path, name, i, total, ratio, pdf):
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
                height = SLIDE_DISPLAY_WIDTH * ratio
                self.pending[path].append(Item("image", st["x"], st["y"], width=SLIDE_DISPLAY_WIDTH,
                                               ratio=ratio, pdf=(pdf, i - 1)))
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
            if item.kind == "cover":                     # a patch over PDF text: width and height
                return item.x, item.y, item.width, item.size
            if item.kind == "sign":
                return item.x, item.y, item.width, item.width * item.sign["aspect"]
            if item.kind == "image":
                ratio = item.ratio or item.texture.get_height() / item.texture.get_width()
                return item.x, item.y, item.width, item.width * ratio
            pw, ph = self.make_layout(item).get_pixel_size()
            return item.x, item.y, max(pw, 8), max(ph, 8)

        def hit_item(self, cx, cy, kinds=("text", "sign", "cover", "image")):
            pad = 4 / self.zoom
            for kind in kinds:                       # text is drawn above pictures
                for it in reversed(self.items):
                    if it.kind != kind:
                        continue
                    bx, by, bw, bh = self.item_box(it)
                    if bx - pad <= cx <= bx + bw + pad and by - pad <= cy <= by + bh + pad:
                        return it
            return None

        def on_text_size(self, scale):
            self.text_px = scale.get_value()
            sel = self.selected
            if sel is not None and sel in self.items and sel.kind == "text":
                sel.size = self.text_px / self.zoom
                self.dirty = True
                self.area.queue_draw()

        def page_colours(self, page, rect):
            """(background, ink) colours of the page picture around and inside rect, as 0..1 triples."""
            import cairo
            surf = cairo.ImageSurface.create_from_png(io.BytesIO(render_pdf_page(page.pdf[0], page.pdf[1] + 1)))
            data, stride, iw, ih = surf.get_data(), surf.get_stride(), surf.get_width(), surf.get_height()
            bx, by, bw, _ = self.item_box(page)

            def px(wx, wy):
                i, j = min(max(int((wx - bx) * iw / bw), 0), iw - 1), min(max(int((wy - by) * iw / bw), 0), ih - 1)
                o = j * stride + 4 * i                      # stored as blue, green, red, alpha
                return data[o + 2] / 255, data[o + 1] / 255, data[o] / 255
            x, y, w, h = rect
            ring = ([px(x - 3, y + h * t) for t in (0.2, 0.5, 0.8)] + [px(x + w + 3, y + h * t) for t in (0.2, 0.5, 0.8)]
                    + [px(x + w * t, y - 3) for t in (0.1, 0.5, 0.9)] + [px(x + w * t, y + h + 3) for t in (0.1, 0.5, 0.9)])
            bg = tuple(sorted(c[k] for c in ring)[len(ring) // 2] for k in range(3))
            far = max((px(x + w * a / 24, y + h * b / 6) for a in range(25) for b in range(1, 6)),
                      key=lambda c: sum(abs(c[k] - bg[k]) for k in range(3)))
            ink = far if sum(abs(far[k] - bg[k]) for k in range(3)) > 0.4 else (0.08, 0.08, 0.08)
            return bg, ink

        def line_cover(self, page, wx, wy):
            """The line of the PDF's own text under a point: what Edit text needs to patch and retype it."""
            src, idx = page.pdf
            if (src, idx) not in self.line_cache:
                self.line_cache[(src, idx)] = pdf_text_lines(src, idx + 1)
            pw, _ph, lines = self.line_cache[(src, idx)]
            if not pw:
                return None
            bx, by, bw, _ = self.item_box(page)
            k, pad = bw / pw, 2 / self.zoom
            for text, x0, y0, x1, y1 in lines:
                r = (bx + x0 * k, by + y0 * k, (x1 - x0) * k, (y1 - y0) * k)
                if r[0] - pad <= wx <= r[0] + r[2] + pad and r[1] - pad <= wy <= r[1] + r[3] + pad:
                    bg, ink = self.page_colours(page, r)
                    return {"rect": r, "text": text, "bg": bg, "fg": ink, "font": r[3] / 1.17}
            return None

        def edit_text_begin(self, x, y):
            wx, wy = self.to_world(x, y)
            if self.hit_item(wx, wy, kinds=("text",)):
                self.text_begin(x, y)                        # text you typed: edit it as usual
                return
            page = next((p for p in self.pdf_pages()
                         if self.item_box(p)[0] <= wx <= self.item_box(p)[0] + self.item_box(p)[2]
                         and self.item_box(p)[1] <= wy <= self.item_box(p)[1] + self.item_box(p)[3]), None)
            if page is None:
                self.status.set_text("Click a line of text on an open PDF page.")
                return
            cover = self.line_cover(page, wx, wy)
            if cover is None:
                self.status.set_text("No text there. (A scanned page is a picture: use the Text tool to write over it.)")
                return
            self.text_begin(x, y, cover)

        def text_begin(self, x, y, cover=None):
            wx, wy = self.to_world(x, y)
            self.text_target = None if cover else self.hit_item(wx, wy, kinds=("text",))
            self.text_cover = cover
            self.text_pos = (cover["rect"][0], cover["rect"][1]) if cover else (wx, wy)
            self.textview.get_buffer().set_text(cover["text"] if cover else
                                                (self.text_target.text if self.text_target else ""))
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
            cover, self.text_cover = self.text_cover, None
            if self.text_cancel:
                return
            self.commit_text(target, self.text_pos, text, cover)
            self.area.grab_focus()

        def commit_text(self, target, pos, text, cover=None):
            """Create a text box (target None) or change/delete an existing one. With `cover` (a line of
            the PDF's own text) the old line is patched over with the page colour and the new text goes on top."""
            if cover is not None:
                if text != cover["text"]:                    # unchanged: leave the page alone
                    x, y, w, h = cover["rect"]
                    self.push_undo()
                    self.items.append(Item("cover", x - 1, y - 1, width=w + 2, size=h + 2, color=cover["bg"],
                                           text=cover["text"]))        # the words it replaced
                    if text:
                        t = Item("text", x, y, text=text, size=cover["font"], color=cover["fg"])
                        self.items.append(t)
                        if is_rtl(text):                     # Hebrew and Arabic lines are anchored on the right
                            t.x = x + w - self.item_box(t)[2]
            elif target is not None and target in self.items:
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
            if not erase and self.mode == "edittext":
                self.gesture_tool = "text"                      # nothing to drag: one click
                self.edit_text_begin(x, y)
                return
            wx, wy = self.to_world(x, y)
            self.erasing = erase or self.mode == "eraser"
            self.gesture_tool = "erase" if self.erasing else "draw"
            if self.erasing:
                self.erase_snapped = False     # Undo step is taken at the first real erase
                self.erase_at(wx, wy)
            else:
                self.push_undo()
                # divide by zoom so the pen keeps the same size on screen whatever the zoom
                if self.mode == "highlight":
                    self.current = Stroke(HIGHLIGHT, 16 * 1.1 / self.zoom, 0.4)
                    pressure = 0.5                         # a highlighter has one width
                else:
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
                self.current.pts.append((wx, wy, 0.5 if self.current.alpha < 1 else pressure))
            self.area.queue_draw()

        def finish(self):
            self.current = None
            self.grab = None

        def erase_at(self, wx, wy):
            r = self.eraser_px * 1.1 / self.zoom  # same size on screen whatever the zoom
            def hit(s):   # near any point, or near the line between two points
                pts = s.pts
                if len(pts) == 1:
                    return math.hypot(pts[0][0] - wx, pts[0][1] - wy) < r
                return any(seg_dist(wx, wy, a[0], a[1], b[0], b[1]) < r for a, b in zip(pts, pts[1:]))
            kept = [s for s in self.strokes if not hit(s)]
            if len(kept) != len(self.strokes):
                if not self.erase_snapped:     # erasing nothing leaves no Undo step
                    self.push_undo()
                    self.erase_snapped = True
                self.strokes = kept

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
            elif it.kind == "cover":
                it.width, it.size = max(cx - it.x, 4.0), max(cy - it.y, 4.0)
            elif it.kind in ("image", "sign"):
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
            self.draw_content(snapshot, view=(-self.ox / self.zoom, -self.oy / self.zoom,
                                              w / self.zoom, h / self.zoom))
            snapshot.restore()
            snapshot.pop()

        def page_texture_sync(self, key):
            tex = self.tex_cache.get(key)
            if tex is None:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(render_pdf_page(key[0], key[1] + 1)))
                self.tex_cache[key] = tex
            return tex

        def page_texture(self, key):
            """A PDF page's picture if it is ready; otherwise ask for it and draw a grey page meanwhile."""
            tex = self.tex_cache.get(key)
            if tex is not None:
                self.tex_cache.move_to_end(key)
                return tex
            if key not in self.tex_pending and key not in self.tex_failed:
                self.tex_pending.add(key)
                self.render_pool.submit(self.render_job, key)
            return None

        def render_job(self, key):                                 # runs on a worker thread
            try:
                data = render_pdf_page(key[0], key[1] + 1)
            except Exception as e:  # noqa: BLE001
                log("page render failed:", e)
                data = None
            GLib.idle_add(self.page_rendered, key, data)

        def page_rendered(self, key, data):
            self.tex_pending.discard(key)
            if data is None:
                self.tex_failed.add(key)
            else:
                self.tex_cache[key] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                while len(self.tex_cache) > PAGE_TEXTURES:
                    self.tex_cache.popitem(last=False)
            self.area.queue_draw()
            return False

        def draw_picture(self, snapshot, it, export, view):
            x, y, w, h = self.item_box(it)
            if view and not (x <= view[0] + view[2] and x + w >= view[0] and y <= view[1] + view[3] and y + h >= view[1]):
                return                                             # off screen: nothing to draw or load
            tex = it.texture
            if tex is None and it.pdf:
                if not export:
                    tex = self.page_texture(it.pdf)
                elif it.pdf in self.export_pages:
                    tex = self.page_texture_sync(it.pdf)
            rect = Graphene.Rect().init(x, y, w, h)
            if tex is not None:
                snapshot.append_texture(tex, rect)
            else:                                                  # a page not drawn yet
                grey = Gdk.RGBA()
                grey.red = grey.green = grey.blue = 0.93
                grey.alpha = 1.0
                snapshot.append_color(grey, rect)

        def draw_content(self, snapshot, export=False, view=None):
            """Everything on the surface, in world pixels (the caller sets up the transform)."""
            for it in self.items:                                  # pictures, under the pen
                if it.kind == "image":
                    self.draw_picture(snapshot, it, export, view)
            if not export:                                         # slides still arriving
                for slides in self.pending.values():
                    for it in slides:
                        self.draw_picture(snapshot, it, export, view)
            for it in self.items:                                  # patches over edited PDF text, under the pen
                if it.kind == "cover":
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue = it.color
                    rgba.alpha = 1.0
                    snapshot.append_color(rgba, Graphene.Rect().init(it.x, it.y, it.width, it.size))
            for s in self.strokes:
                rgba = Gdk.RGBA()
                rgba.red, rgba.green, rgba.blue = s.color
                rgba.alpha = s.alpha
                if s.cache is None or s.cache[0] != len(s.pts):    # only the stroke being drawn changes
                    s.cache = (len(s.pts), stroke_runs(s))
                for path, stroke in s.cache[1]:
                    snapshot.append_stroke(path, stroke, rgba)
            for it in self.items:                                  # signatures, inked like the pen
                if it.kind == "sign":
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue = it.color
                    rgba.alpha = 1.0
                    for st in sign_strokes(it):
                        for path, stroke in stroke_runs(st):
                            snapshot.append_stroke(path, stroke, rgba)
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

        def get_renderer(self):
            if self.export_renderer is None:
                renderer = Gsk.CairoRenderer.new()
                renderer.realize_for_display(Gdk.Display.get_default())
                self.export_renderer = renderer
            return self.export_renderer

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
            self.export_pages = {it.pdf for it in self.pdf_pages()[:EXPORT_MAX_PAGES]}
            self.draw_content(snap, export=True)
            node = snap.to_node()
            # A private offscreen renderer: the window's own renderer was missing in a long-lived
            # session ("'NoneType' object has no attribute 'render_texture'"), which broke saving.
            texture = self.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, iw, ih))
            tmp = f"{path}.tmp"                      # write aside, then swap in: never a half-written PNG
            if not texture.save_to_png(tmp):
                raise OSError("could not write the PNG")
            os.replace(tmp, path)
            return iw, ih

        # ---- PDF: pages, page tools, saving as PDF -------------------------------
        def pdf_pages(self):
            """PDF page pictures in document order: columns left to right, top to bottom."""
            return sorted((i for i in self.items if i.kind == "image" and i.pdf),
                          key=lambda i: (round(i.x), i.y))

        def target_page(self):
            """The page a page tool acts on: the selected one, else the one nearest the view centre."""
            pages = self.pdf_pages()
            if not pages:
                self.status.set_text("Open a PDF first (left panel: PDF…).")
                return None
            if self.selected in pages:
                return self.selected
            w, h = self.canvas_size()
            cx, cy = self.to_world(w / 2, h / 2)

            def away(i):
                bx, by, bw, bh = self.item_box(i)
                return math.hypot(bx + bw / 2 - cx, by + bh / 2 - cy)
            return min(pages, key=away)

        def transform_marks(self, rules):
            """rules = [(rect, f)]. Strokes and text whose centre is inside a rect are moved by
            f(x, y) -> (x, y). Each mark follows the first rect that holds it, judged before any move."""
            def rule_for(px, py):
                for (x, y, w, h), f in rules:
                    if x <= px <= x + w and y <= py <= y + h:
                        return f
                return None
            marks = []
            for st in self.strokes:
                f = rule_for(*stroke_centre(st))
                if f:
                    moved = Stroke(st.color, st.width, st.alpha)
                    moved.pts = [(*f(px, py), pr) for px, py, pr in st.pts]
                    st = moved
                marks.append(st)
            self.strokes = marks
            for it in self.items:
                if it.kind == "text":
                    f = rule_for(it.x, it.y)
                    if f:
                        it.x, it.y = f(it.x, it.y)
                elif it.kind in ("sign", "cover"):           # these follow by their centre
                    cx, cy = self.item_centre(it)
                    f = rule_for(cx, cy)
                    if f:
                        nx, ny = f(cx, cy)
                        it.x, it.y = it.x + nx - cx, it.y + ny - cy

        def delete_page(self, *_):
            it = self.target_page()
            if it is None:
                return
            x, y, w, h = self.item_box(it)
            self.push_undo()

            def inside(px, py):
                return x <= px <= x + w and y <= py <= y + h
            self.strokes = [st for st in self.strokes if not inside(*stroke_centre(st))]
            self.items = [i for i in self.items
                          if i is not it and not (i.kind == "text" and inside(i.x, i.y))
                          and not (i.kind in ("sign", "cover") and inside(*self.item_centre(i)))]
            below = [p for p in self.pdf_pages() if abs(p.x - it.x) < 1 and p.y > it.y]
            if below:                                        # pull the pages underneath up
                dy = it.y - below[0].y
                self.transform_marks([(self.item_box(p), lambda px, py: (px, py + dy)) for p in below])
                for p in below:
                    p.y += dy
            self.selected = None
            self.status.set_text("Page deleted. Undo brings it back.")
            self.area.queue_draw()

        def move_page(self, step):
            it = self.target_page()
            if it is None:
                return
            col = [p for p in self.pdf_pages() if abs(p.x - it.x) < 1]
            j = col.index(it) + step
            if not 0 <= j < len(col):
                self.status.set_text("That page is already " + ("first." if step < 0 else "last."))
                return
            a, b = (col[j], it) if step < 0 else (it, col[j])      # a is above b
            ra, rb = self.item_box(a), self.item_box(b)
            gap = b.y - (a.y + ra[3])
            da, db = rb[3] + gap, a.y - b.y                        # a goes down, b goes up
            self.push_undo()
            self.transform_marks([(ra, lambda px, py: (px, py + da)), (rb, lambda px, py: (px, py + db))])
            a.y, b.y = a.y + da, b.y + db
            self.status.set_text("Page moved.")
            self.area.queue_draw()

        def unturnable(self, it):
            """Marks on a page that can't turn with it: typed text, signatures, patches over edited text."""
            x, y, w, h = self.item_box(it)

            def inside(px, py):
                return x <= px <= x + w and y <= py <= y + h
            return [i for i in self.items if (i.kind == "text" and inside(i.x, i.y))
                    or (i.kind in ("sign", "cover") and inside(*self.item_centre(i)))]

        def rotate_page(self, *_):
            it = self.target_page()
            if it is None:
                return
            from pypdf import PdfWriter
            src, idx = it.pdf
            x, y, w, h = self.item_box(it)
            loose = self.unturnable(it)
            flat = new = None
            try:
                PDF_STORE.mkdir(parents=True, exist_ok=True)
                if loose:          # make the page's marks part of the page itself, so the turn carries them
                    flat = PDF_STORE / f"flat-{os.getpid()}-{time.time_ns()}.pdf"
                    self.export_pdf(flat, [self.pdf_pages().index(it) + 1])
                    src, idx = str(flat), 0
                writer = PdfWriter(clone_from=src)
                writer.pages[idx].rotate(90)
                tmp = PDF_STORE / f"rot-{os.getpid()}-{time.time_ns()}.pdf"
                with open(tmp, "wb") as f:
                    writer.write(f)
                new = PDF_STORE / f"{file_sha1(tmp)}.pdf"
                os.replace(tmp, new)
            except Exception as e:  # noqa: BLE001
                log("rotate failed:", e)
                self.status.set_text(f"Could not rotate: {e}")
                return
            finally:
                if flat:
                    flat.unlink(missing_ok=True)
            self.push_undo()

            def inside(px, py):
                return x <= px <= x + w and y <= py <= y + h
            if loose:              # the marks are in the page now: drop the loose copies
                self.strokes = [st for st in self.strokes if not inside(*stroke_centre(st))]
                self.items = [i for i in self.items if not any(i is m for m in loose)]
            # a clockwise quarter turn: a point u across and v down the page lands h - v across, u down
            self.transform_marks([((x, y, w, h), lambda px, py: (x + h - (py - y), y + (px - x)))])
            it.pdf, it.width, it.ratio = (str(new), idx), h, 1 / it.ratio
            self.status.set_text("Page turned. Undo turns it back."
                                 + (" Its text, signature and marks are now part of the page." if loose else ""))
            self.area.queue_draw()

        # ---- signing: draw it once, place it on any page ------------------------
        def sign_file(self):
            return Path(os.environ.get("PEN_NOTE_CONFIG") or Path.home() / ".config" / "pen-note") / "signature.json"

        def on_sign(self, *_):
            try:
                sig = json.loads(self.sign_file().read_text())
            except (OSError, ValueError):
                sig = None
            if sig:
                self.place_signature(sig)
            else:
                self.start_signature()

        def start_signature(self, *_):
            self.cancel_signature()                          # starting again drops a drawing in progress
            self.signing = {"strokes": list(self.strokes), "undo": list(self.undo_stack),
                            "redo": list(self.redo_stack), "dirty": self.dirty}
            self.set_mode("pen")
            self.sign_row.set_visible(True)
            self.status.set_text("Draw your signature anywhere, then click Done. It is saved, so you only do this once.")

        def cancel_signature(self, *_):
            """Leave signature drawing and put the surface back as it was."""
            if self.signing is None:
                return
            before, self.signing = self.signing, None
            self.strokes, self.undo_stack, self.redo_stack = before["strokes"], before["undo"], before["redo"]
            self.dirty = before["dirty"]
            self.sign_row.set_visible(False)
            self.status.set_text("")
            self.area.queue_draw()

        def finish_signature(self, *_):
            if self.signing is None:
                return
            known = {id(st) for st in self.signing["strokes"]}
            mine = [st for st in self.strokes if id(st) not in known]
            if not mine:
                self.status.set_text("Draw your signature first, then click Done.")
                return
            half = max(st.width for st in mine) / 2
            x0 = min(p[0] for st in mine for p in st.pts) - half
            y0 = min(p[1] for st in mine for p in st.pts) - half
            x1 = max(p[0] for st in mine for p in st.pts) + half
            y1 = max(p[1] for st in mine for p in st.pts) + half
            bw = max(x1 - x0, 1.0)
            sig = {"aspect": max(y1 - y0, 1.0) / bw,
                   "strokes": [{"width": st.width / bw,
                                "pts": [[(px - x0) / bw, (py - y0) / bw, pr] for px, py, pr in st.pts]}
                               for st in mine]}
            self.cancel_signature()                          # the drawing becomes a signature, not strokes
            try:
                self.sign_file().parent.mkdir(parents=True, exist_ok=True)
                self.sign_file().write_text(json.dumps(sig))
            except OSError as e:
                log("could not save the signature:", e)
            self.place_signature(sig)

        def place_signature(self, sig):
            w, h = self.canvas_size()
            width = 220 / self.zoom
            cx, cy = self.to_world(w / 2, h / 2)
            self.push_undo()
            it = Item("sign", cx - width / 2, cy - width * sig["aspect"] / 2, width=width,
                      color=self.color, sign=sig)
            self.items.append(it)
            self.set_mode("move")
            self.selected = it
            self.status.set_text("Signature placed: drag it into position, drag the corner square to resize. "
                                 "Pick a colour to go back to drawing.")
            self.area.queue_draw()

        def on_split(self, *_):
            pages = self.pdf_pages()
            if not pages:
                self.status.set_text("Open a PDF first (left panel: PDF…).")
                return
            dlg = Gtk.Window(title="Split", transient_for=self.win, modal=True, default_width=340)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
            msg = Gtk.Label(label=f"This PDF has {len(pages)} pages. Which of them go into the new file?\n"
                                  "For example 1-3,5", xalign=0)
            entry = Gtk.Entry(placeholder_text="1-3,5", activates_default=True)
            ok = Gtk.Button(label="Choose where to save…")
            ok.add_css_class("suggested-action")

            def go(*_):
                try:
                    chosen = parse_pages(entry.get_text(), len(pages))
                except ValueError as e:
                    msg.set_text(str(e))
                    return
                dlg.close()
                self.ask_pdf_path(chosen)
            ok.connect("clicked", go)
            entry.connect("activate", go)
            for w in (msg, entry, ok):
                box.append(w)
            dlg.set_child(box)
            dlg.present()

        def on_save_pdf(self, *_):
            self.ask_pdf_path(None)

        def ask_pdf_path(self, only):
            if not (self.strokes or self.items):
                self.status.set_text("Nothing to save yet.")
                return
            dlg = Gtk.FileDialog(title="Save as PDF" if not only else "Save the chosen pages as PDF")
            name = safe_name(self.name_entry.get_text()) or self.saved_base or "Pen note"
            dlg.set_initial_name(f"{name}{'' if not only else ' (part)'}.pdf")
            if NOTES_DIR.is_dir():
                dlg.set_initial_folder(Gio.File.new_for_path(str(NOTES_DIR)))

            def done(d, res):
                try:
                    f = d.save_finish(res)
                except GLib.Error:
                    return                                   # cancelled
                path = Path(f.get_path())
                if path.suffix.lower() != ".pdf":
                    path = path.with_name(path.name + ".pdf")
                try:
                    r = self.export_pdf(path, only)
                    msg = f"Saved PDF: {path}"
                    if r["covers"] and not r["left"]:
                        msg += ". The text you edited is removed from the file, not just hidden."
                    elif r["left"]:
                        msg += (f". {r['left']} edited line(s) are only covered: this PDF keeps that text inside, "
                                "hidden. Don't count on it to hide anything.")
                    self.status.set_text(msg)
                except Exception as e:  # noqa: BLE001
                    log("pdf export failed:", e)
                    self.status.set_text(f"PDF failed: {e}")
            dlg.save(self.win, None, done)

        def all_strokes(self):
            """Pen strokes plus the strokes of every signature."""
            return self.strokes + [st for it in self.items if it.kind == "sign" for st in sign_strokes(it)]

        def item_centre(self, it):
            x, y, w, h = self.item_box(it)
            return x + w / 2, y + h / 2

        def marks_in(self, rect):
            """True if any stroke or text sits on the rectangle (so a PDF page needs an overlay)."""
            x, y, w, h = rect
            for st in self.all_strokes():
                xs, ys = [p[0] for p in st.pts], [p[1] for p in st.pts]
                if min(xs) <= x + w and max(xs) >= x and min(ys) <= y + h and max(ys) >= y:
                    return True
            for it in self.items:
                if it.kind in ("text", "cover"):
                    bx, by, bw, bh = self.item_box(it)
                    if bx <= x + w and bx + bw >= x and by <= y + h and by + bh >= y:
                        return True
            return False

        def draw_cairo(self, ctx, rect, images):
            """Draw strokes and text (and pictures, if asked) into a cairo context set up in world
            pixels, clipped to rect. Strokes stay vector lines; text goes in as a sharp image."""
            import cairo
            x, y, w, h = rect
            ctx.save()
            ctx.rectangle(x, y, w, h)
            ctx.clip()
            if images:
                for it in self.items:
                    if it.kind == "image":
                        surf = cairo.ImageSurface.create_from_png(io.BytesIO(texture_png(it)))
                        bx, by, bw, _ = self.item_box(it)
                        ctx.save()
                        ctx.translate(bx, by)
                        ctx.scale(bw / surf.get_width(), bw / surf.get_width())
                        ctx.set_source_surface(surf, 0, 0)
                        ctx.paint()
                        ctx.restore()
            for it in self.items:                                  # patches first: they hide the old text
                if it.kind == "cover":
                    ctx.set_source_rgb(*it.color)
                    ctx.rectangle(it.x, it.y, it.width, it.size)
                    ctx.fill()
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            for st in self.all_strokes():
                ctx.set_source_rgba(*st.color, st.alpha)
                for width, segs in stroke_segments(st):
                    ctx.set_line_width(width)
                    ctx.move_to(*segs[0][0])
                    for _, c1, c2, p2 in segs:
                        ctx.curve_to(*c1, *c2, *p2)
                    ctx.stroke()
            for it in self.items:                                  # typed text
                if it.kind != "text":
                    continue
                ctx.save()
                ctx.translate(it.x, it.y)
                ctx.set_source_rgb(*it.color)
                if PangoCairo is not None:                         # real text: selectable and searchable
                    layout = PangoCairo.create_layout(ctx)
                    layout.set_text(it.text, -1)
                    layout.set_font_description(self.make_layout(it).get_font_description())
                    PangoCairo.show_layout(ctx, layout)
                else:                                              # no python3-gi-cairo: sharp outlines instead
                    pb = Gsk.PathBuilder.new()
                    pb.add_layout(self.make_layout(it))

                    def seg(op, pts, n, weight, _data=None):
                        kind = op.value_nick
                        if kind == "move":
                            ctx.move_to(pts[0].x, pts[0].y)
                        elif kind == "line":
                            ctx.line_to(pts[1].x, pts[1].y)
                        elif kind == "cubic":
                            ctx.curve_to(pts[1].x, pts[1].y, pts[2].x, pts[2].y, pts[3].x, pts[3].y)
                        elif kind == "close":
                            ctx.close_path()
                        return True
                    pb.to_path().foreach(Gsk.PathForeachFlags.CUBIC, seg)
                    ctx.fill()
                ctx.restore()
            ctx.restore()

        def export_pdf(self, path, only=None):
            """Opened PDF pages are kept as they are (text stays real text) with the marks laid on
            top. With no PDF open, the whole surface becomes one page. `only` = page numbers to keep."""
            import cairo
            from pypdf import PdfReader, PdfWriter
            pages = self.pdf_pages()
            if not pages:
                return self.export_canvas_pdf(path)
            if only:
                pages = [pages[n - 1] for n in only]
            readers, writer, probe, checks = {}, PdfWriter(), PdfWriter(), []
            for it in pages:
                src, idx = it.pdf
                if src not in readers:
                    readers[src] = PdfReader(src)
                    if readers[src].is_encrypted:
                        readers[src].decrypt("")
                page = writer.add_page(readers[src].pages[idx])   # attached first: edits to it are reliable
                page.transfer_rotation_to_content()                # so the marks need no rotation maths
                box = self.item_box(it)
                media, crop = page.mediabox, page.cropbox
                k = float(crop.width) / box[2]                     # points per world pixel
                covers = [c for c in self.items if c.kind == "cover"
                          and box[0] <= self.item_centre(c)[0] <= box[0] + box[2]
                          and box[1] <= self.item_centre(c)[1] <= box[1] + box[3]]
                if covers:                                         # take the old text out of the page itself
                    strip_text_in_rects(page, [
                        (float(crop.left) + (c.x - box[0]) * k, float(crop.top) - (c.y + c.size - box[1]) * k,
                         float(crop.left) + (c.x + c.width - box[0]) * k, float(crop.top) - (c.y - box[1]) * k)
                        for c in covers])
                    probe.add_page(page)                           # a copy as it is now: old text out, nothing on top yet
                    checks.append((len(probe.pages),
                                   [((c.x - box[0]) * k, (c.y - box[1]) * k, c.width * k, c.size * k) for c in covers]))
                if self.marks_in(box):
                    buf = io.BytesIO()
                    surf = cairo.PDFSurface(buf, float(media.width), float(media.height))
                    ctx = cairo.Context(surf)
                    ctx.translate(float(crop.left - media.left), float(media.top - crop.top))
                    ctx.scale(k, k)
                    ctx.translate(-box[0], -box[1])
                    self.draw_cairo(ctx, box, images=False)
                    surf.finish()
                    page.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                writer.write(f)
            os.replace(tmp, path)
            result = {"covers": sum(len(r) for _, r in checks), "left": 0}
            if checks:                                             # is any of the old text still in those pages?
                tmp_probe = f"{path}.probe"
                with open(tmp_probe, "wb") as f:
                    probe.write(f)
                try:
                    for pageno, rects in checks:
                        lines = pdf_text_lines(tmp_probe, pageno)[2]
                        for rx, ry, rw, rh in rects:
                            if any(max(0.0, min(x1, rx + rw) - max(x0, rx)) * max(0.0, min(y1, ry + rh) - max(y0, ry))
                                   > 0.25 * rw * rh for _t, x0, y0, x1, y1 in lines):
                                result["left"] += 1
                finally:
                    os.unlink(tmp_probe)
            return result

        def export_canvas_pdf(self, path):
            import cairo
            bx, by, bw, bh = self.content_bounds()
            k = min(0.75, 14000 / max(bw, bh))                     # 96 px per inch, within the PDF page limit
            tmp = f"{path}.tmp"
            surf = cairo.PDFSurface(tmp, bw * k, bh * k)
            ctx = cairo.Context(surf)
            ctx.scale(k, k)
            ctx.translate(-bx, -by)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            self.draw_cairo(ctx, (bx, by, bw, bh), images=True)
            surf.finish()
            os.replace(tmp, path)
            return {"covers": 0, "left": 0}

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
                    if it.sign:
                        d["sign"] = it.sign
                    if it.pdf:                          # the original PDF travels with the note
                        name = pdf_member(it.pdf[0])
                        if name not in zf.namelist():
                            zf.write(it.pdf[0], name, compress_type=zipfile.ZIP_STORED)
                        d["pdf"] = {"file": name, "page": it.pdf[1]}
                        d["ratio"] = it.ratio
                    if it.kind == "image" and not it.pdf:       # a PDF page is drawn again from its PDF
                        d["image"] = f"images/{n}.png"
                        zf.writestr(d["image"], texture_png(it))
                    items.append(d)
                state = {"version": 2,
                         "view": {"zoom": self.zoom, "ox": self.ox, "oy": self.oy},
                         "strokes": [{"color": list(s.color), "width": s.width, "alpha": s.alpha,
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
                    s = Stroke(tuple(sd["color"]), sd["width"], sd.get("alpha", 1.0))
                    s.pts = [tuple(p) for p in sd["pts"]]
                    strokes.append(s)
                items = []
                for d in state["items"]:
                    it = Item(d["kind"], d["x"], d["y"], width=d["width"], text=d["text"],
                              size=d["size"], color=tuple(d["color"]), auto=d.get("auto", False),
                              sign=d.get("sign"))
                    if "pdf" in d:
                        PDF_STORE.mkdir(parents=True, exist_ok=True)
                        out = PDF_STORE / Path(d["pdf"]["file"]).name
                        if not out.exists():
                            out.write_bytes(zf.read(d["pdf"]["file"]))
                        os.utime(out)
                        it.pdf = (str(out), d["pdf"]["page"])
                        it.ratio = d.get("ratio")
                        old = None
                        if "image" in d:                            # saved when pages were kept as pictures
                            iw, ih = struct.unpack(">II", zf.read(d["image"])[16:24])
                            old = ih / iw
                        if old is not None or not it.ratio:
                            migrate_page(it, old)
                    elif d["kind"] == "image":
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
            self.show_note(strokes, items, view, base)
            self.status.set_text(f"Opened “{base}”" + ("" if proj.exists() else " (image only: draw on top of it)"))
            return True

        def show_note(self, strokes, items, view, base):
            """Put a loaded note on the surface. base=None: not saved in the vault yet."""
            self.strokes, self.items, self.selected = strokes, items, None
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.saved_base = base
            self.name_entry.set_text(base or "")
            self.dirty = False
            self.close_warned = False
            self.set_mode("pen")
            self.set_view(*view)
            self.win.set_title(f"Pen Note: {base}" if base else "Pen Note")
            self.refresh_recent()

        def open_project_file(self, p):
            """Open a .pennote file. One from the vault continues that note; any other is a copy
            that Save puts into the vault as a new note."""
            p = Path(p).resolve()
            if p.parent == PROJECT_DIR.resolve() and (NOTES_DIR / f"{p.stem}.png").exists():
                self.open_note(p.stem)
                return
            if self.dirty and (self.has_content() or self.saved_base):
                self.save()                                  # don't lose what's on screen
            try:
                strokes, items, view = self.read_project(p)
            except Exception as e:  # noqa: BLE001
                log("open failed:", e)
                self.status.set_text(f"Could not open “{p.name}”: {e}")
                return
            self.show_note(strokes, items, view, None)
            self.name_entry.set_text(safe_name(p.stem))
            self.dirty = True                                # not in the vault yet: Save/close writes it
            self.status.set_text(f"Opened “{p.name}”. Save adds it to your Pen Notes.")

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

                ensure_edit_link(md, base)
                if old and old != base:                      # drop the old name's files
                    for f in (NOTES_DIR / f"{old}.md", NOTES_DIR / f"{old}.png",
                              PROJECT_DIR / f"{old}.pennote"):
                        f.unlink(missing_ok=True)
                self.saved_base = base
                self.dirty = False
                self.win.set_title(f"Pen Note: {base}")
                self.status.set_text(f"Saved: Pen Notes/{base}")
                self.refresh_recent()
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
            self.render_pool.shutdown(wait=False, cancel_futures=True)
            if screenshot:
                Path(screenshot).unlink(missing_ok=True)
            return False

    app = App()
    return app.run([sys.argv[0], *files])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("daemon")
    c = sub.add_parser("canvas")
    c.add_argument("--screenshot")
    c.add_argument("--open", dest="open_name")
    c.add_argument("--slides", help="a PowerPoint/OpenDocument/PDF file to lay out on the canvas")
    c.add_argument("files", nargs="*", help="notes (.pennote), pictures or slides to open")
    args = ap.parse_args()
    if args.mode == "daemon":
        run_daemon()
    else:
        sys.exit(run_canvas(args.screenshot, args.open_name, args.slides, args.files))


if __name__ == "__main__":
    main()
