"""Inkling's converter: turns almost any file into almost any other.

It does not know a fixed list of "A to B" tricks. It knows what each tool on the computer can do (the office apps,
poppler, Pillow, ffmpeg if installed, ...), draws all of that as a map of formats, and finds the best route
between two of them (for example Word -> PDF -> one picture per page), preferring direct routes, avoiding steps
that lose a lot, and running many files at once.

The engine has no window and no GTK: `Converter(host)` takes the Inkling module as `host` for the office apps and the
PDF-to-Word/PowerPoint builders.
"""
import base64
import heapq
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# id -> (name shown, kind, file endings; the first is used for results)
FORMATS = {
    "docx": ("Word (.docx)", "document", ["docx"]),
    "doc": ("Word 97 (.doc)", "document", ["doc"]),
    "odt": ("OpenDocument text (.odt)", "document", ["odt"]),
    "rtf": ("Rich text (.rtf)", "document", ["rtf"]),
    "html": ("Web page (.html)", "document", ["html", "htm"]),
    "md": ("Markdown (.md)", "document", ["md", "markdown"]),
    "tex": ("LaTeX (.tex)", "document", ["tex"]),
    "txt": ("Plain text (.txt)", "document", ["txt", "text"]),
    "pdf": ("PDF", "document", ["pdf"]),
    "epub": ("E-book (.epub)", "document", ["epub"]),
    "pptx": ("PowerPoint (.pptx)", "slides", ["pptx"]),
    "ppsx": ("PowerPoint show (.ppsx)", "slides", ["ppsx"]),
    "deckcraft": ("DeckCraft (.deckcraft)", "slides", ["deckcraft"]),
    "xlsx": ("Excel (.xlsx)", "spreadsheet", ["xlsx"]),
    "csv": ("CSV table (.csv)", "spreadsheet", ["csv"]),
    "tsv": ("TSV table (.tsv)", "spreadsheet", ["tsv"]),
    "json": ("JSON (.json)", "data", ["json"]),
    "yaml": ("YAML (.yaml)", "data", ["yaml", "yml"]),
    "toml": ("TOML (.toml)", "data", ["toml"]),
    "png": ("PNG picture (.png)", "image", ["png"]),
    "jpg": ("JPEG picture (.jpg)", "image", ["jpg", "jpeg"]),
    "gif": ("GIF (.gif)", "image", ["gif"]),
    "webp": ("WebP picture (.webp)", "image", ["webp"]),
    "bmp": ("Bitmap (.bmp)", "image", ["bmp"]),
    "tiff": ("TIFF picture (.tiff)", "image", ["tiff", "tif"]),
    "ico": ("Icon (.ico)", "image", ["ico"]),
    "svg": ("Vector drawing (.svg)", "image", ["svg"]),
    "heic": ("HEIC photo (.heic)", "image", ["heic", "heif"]),
    "avif": ("AVIF picture (.avif)", "image", ["avif"]),
    "eps": ("EPS drawing (.eps)", "image", ["eps"]),
    "zip": ("Zip archive (.zip)", "archive", ["zip"]),
    "tar": ("Tar archive (.tar)", "archive", ["tar"]),
    "tgz": ("Compressed tar (.tar.gz)", "archive", ["tar.gz", "tgz"]),
    "txz": ("Compressed tar (.tar.xz)", "archive", ["tar.xz", "txz"]),
    "tbz": ("Compressed tar (.tar.bz2)", "archive", ["tar.bz2", "tbz2", "tbz"]),
    "folder": ("Folder", "archive", []),
    "mp3": ("MP3 audio (.mp3)", "audio", ["mp3"]),
    "wav": ("WAV audio (.wav)", "audio", ["wav"]),
    "flac": ("FLAC audio (.flac)", "audio", ["flac"]),
    "ogg": ("Ogg audio (.ogg)", "audio", ["ogg", "oga"]),
    "m4a": ("M4A audio (.m4a)", "audio", ["m4a", "aac"]),
    "opus": ("Opus audio (.opus)", "audio", ["opus"]),
    "mp4": ("MP4 video (.mp4)", "video", ["mp4", "m4v"]),
    "mkv": ("MKV video (.mkv)", "video", ["mkv"]),
    "webm": ("WebM video (.webm)", "video", ["webm"]),
    "mov": ("QuickTime video (.mov)", "video", ["mov"]),
    "avi": ("AVI video (.avi)", "video", ["avi"]),
}
# results that are only ever a last step: id -> (name shown, file ending, one file per page?)
TERMINALS = {"png*": ("PNG pictures, one per page", "png", True), "jpg*": ("JPEG pictures, one per page", "jpg", True),
             "tiff*": ("TIFF pictures, one per page", "tiff", True), "svg*": ("SVG drawings, one per page", "svg", True),
             "eps*": ("EPS drawings, one per page", "eps", True), "gif~": ("Animated GIF, one frame per slide", "gif", False)}
# what each kind of file can sensibly become (so Word never offers to become JSON), and how dear a route may be
ALLOWED = {"document": {"document", "slides", "image"}, "slides": {"slides", "document", "image"},
           "spreadsheet": {"spreadsheet", "data", "document", "image"}, "data": {"data", "spreadsheet", "document", "image"},
           "image": {"image", "document", "slides"}, "archive": {"archive"}, "audio": {"audio"}, "video": {"video", "audio", "image"}}
ALLOWED_EXTRA = {"html": {"spreadsheet", "data"}}       # a web page can hold tables
NO_TEXT_FROM_PICTURES = {"txt", "md", "tex", "html"}    # a picture has no text to read out (there is no reading of letters)
MAX_COST = 6.0
ENDINGS = {e: fid for fid, (_l, _k, ends) in FORMATS.items() for e in ends}
LOSSY_MIDDLES = {"txt", "md"}     # a route should not go through these unless it has to
LOSSY_PENALTY = 4.0
DOC_IN = ["docx", "doc", "odt", "rtf", "html", "md", "tex", "txt"]
DOC_OUT = ["docx", "odt", "rtf", "html", "md", "tex", "txt", "pdf"]
IMAGES = ["png", "jpg", "gif", "webp", "bmp", "tiff", "ico"]
ARCHIVES = ["zip", "tar", "tgz", "txz", "tbz"]
AUDIO = ["mp3", "wav", "flac", "ogg", "m4a", "opus"]
VIDEO = ["mp4", "mkv", "webm", "mov", "avi"]
NOTES = {
    "pdf-to-office": "the text is rebuilt (layout simplified); pages without text stay pictures",
    "pdf-to-html": "the text is rebuilt (layout simplified)",
    "pdftotext": "text only",
    "pdftoppm": "one picture per page", "pdftocairo": "one drawing per page",
    "pillow": "pictures only", "pillow-pdf": "", "pixbuf": "drawn at a fixed size",
    "deckcraft:gif": "an animated picture, one frame per slide",
}


class ConversionError(Exception):
    """Something went wrong converting one file; the message is meant for the person using the app."""


class Cancelled(Exception):
    pass


class Edge:
    __slots__ = ("src", "dst", "cost", "engine", "note")

    def __init__(self, src, dst, cost, engine, note=""):
        self.src, self.dst, self.cost, self.engine, self.note = src, dst, cost, engine, note


def split_name(name):
    """('report', '.pdf'), ('backup', '.tar.gz'): a name without its ending, and the ending."""
    for ending in (".tar.gz", ".tar.xz", ".tar.bz2"):
        if name.lower().endswith(ending):
            return name[: -len(ending)], name[-len(ending):]
    p = Path(name)
    return (p.stem, p.suffix) if p.suffix else (name, "")


def unique_path(folder, name):
    """folder/name, or folder/name (2), (3)… so nothing is overwritten."""
    stem, suffix = split_name(name)
    candidate, n = Path(folder) / name, 2
    while candidate.exists():
        candidate = Path(folder) / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


class Converter:
    def __init__(self, host, dpi=150):
        self.host = host
        self.dpi = dpi
        self.engines = self._engines()
        self.edges = self._build_edges()
        self.out = {}
        for e in self.edges:
            self.out.setdefault(e.src, []).append(e)

    # ---- what this computer can do -------------------------------------------------------------------------------
    def _engines(self):
        h = self.host
        have = {
            "wordcraft": h.office_available("wordcraft"), "deckcraft": h.office_available("deckcraft"),
            "gridcraft": h.office_available("gridcraft"),
            "poppler": bool(shutil.which("pdftoppm") and shutil.which("pdftotext")),
            "pdftocairo": bool(shutil.which("pdftocairo")),
            "ffmpeg": bool(shutil.which("ffmpeg")), "pandoc": bool(shutil.which("pandoc")),
        }
        for name, module in (("pillow", "PIL"), ("pypdf", "pypdf"), ("yaml", "yaml")):
            try:
                __import__(module)
                have[name] = True
            except ImportError:
                have[name] = False
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            have["pixbuf"] = {f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()}
        except Exception:  # noqa: BLE001
            have["pixbuf"] = set()
        return have

    def _build_edges(self):
        E, have = [], self.engines

        def add(srcs, dsts, cost, engine, note=""):
            for s in srcs:
                for d in dsts:
                    if s != d:
                        E.append(Edge(s, d, cost, engine, NOTES.get(note or engine, note)))
        if have["wordcraft"]:
            add(DOC_IN, DOC_OUT, 1.0, "wordcraft")
        if have["deckcraft"]:
            add(["pptx", "ppsx", "deckcraft"], ["pptx", "deckcraft", "pdf", "txt", "md"], 1.0, "deckcraft")
            add(["pptx", "ppsx", "deckcraft"], ["gif~"], 2.0, "deckcraft", NOTES["deckcraft:gif"])
        if have["gridcraft"]:
            add(["xlsx", "csv", "tsv", "html"], ["xlsx", "csv", "tsv", "html", "pdf"], 1.2, "gridcraft")
        if have["poppler"]:
            add(["pdf"], ["png*", "jpg*", "tiff*"], 2.0, "pdftoppm")
            add(["pdf"], ["txt"], 2.0, "pdftotext")
            if have["pypdf"]:
                add(["pdf"], ["html"], 3.0, "pdf-to-html")
            if have["wordcraft"] and have["pypdf"]:
                add(["pdf"], ["docx", "odt", "rtf"], 4.0, "pdf-to-office")
            if have["deckcraft"] and have["pypdf"]:
                add(["pdf"], ["pptx"], 4.0, "pdf-to-office")
        if have["pdftocairo"]:
            add(["pdf"], ["svg*", "eps*"], 2.5, "pdftocairo")
        if have["pillow"]:
            add(IMAGES, IMAGES, 1.0, "pillow")
            add(IMAGES, ["pdf"], 1.5, "pillow-pdf")
        decodable = [f for f in ("svg", "heic", "avif") if f in have["pixbuf"] or (f == "heic" and "heif/avif" in have["pixbuf"])]
        if decodable and have["pillow"]:
            add(decodable, ["png"], 2.0, "pixbuf")
        if have["yaml"]:
            add(["json"], ["yaml"], 1.0, "data")
            add(["yaml"], ["json"], 1.0, "data")
            add(["toml"], ["json", "yaml"], 1.0, "data")
        else:
            add(["toml"], ["json"], 1.0, "data")
        add(["csv", "tsv"], ["json"], 0.8, "table")                      # plain tables <-> a JSON list of records
        add(["json"], ["csv", "tsv"], 0.8, "table")
        add(ARCHIVES + ["folder"], ARCHIVES, 1.0, "archive")
        add(ARCHIVES, ["folder"], 1.0, "archive")
        if have["ffmpeg"]:
            add(AUDIO + VIDEO, AUDIO, 2.0, "ffmpeg")
            add(VIDEO, VIDEO, 2.5, "ffmpeg")
            add(VIDEO, ["gif"], 3.0, "ffmpeg")
            add(["gif"], ["mp4"], 3.0, "ffmpeg")
        if have["pandoc"]:
            add([d for d in DOC_IN if d != "doc"], [d for d in DOC_OUT if d not in ("pdf",)] + ["epub"], 1.5, "pandoc")
        return E

    # ---- recognising a file ------------------------------------------------------------------------------------------
    def detect(self, path):
        """The format id of a file or folder, from its name or, failing that, what is inside. None if unknown."""
        p = Path(path)
        if p.is_dir():
            return "folder"
        name = p.name.lower()
        for ending in sorted(ENDINGS, key=len, reverse=True):          # longest first: ".tar.gz" before ".gz"
            if name.endswith("." + ending):
                return ENDINGS[ending]
        return self._sniff(p)

    @staticmethod
    def _sniff(p):
        try:
            with open(p, "rb") as f:
                head = f.read(4096)
        except OSError:
            return None
        if head.startswith(b"%PDF"):
            return "pdf"
        for magic, fid in ((b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF8", "gif"), (b"BM", "bmp"),
                           (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"\x00\x00\x01\x00", "ico")):
            if head.startswith(magic):
                return fid
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "webp"
        if head[:4] == b"PK\x03\x04":
            try:
                names = set(zipfile.ZipFile(p).namelist())
            except zipfile.BadZipFile:
                return None
            for marker, fid in (("word/document.xml", "docx"), ("ppt/presentation.xml", "pptx"), ("xl/workbook.xml", "xlsx")):
                if marker in names:
                    return fid
            if "mimetype" in names:
                mime = zipfile.ZipFile(p).read("mimetype")[:60]
                if b"opendocument.text" in mime:
                    return "odt"
            return "zip"
        if head[:2] == b"\x1f\x8b":
            return "tgz"
        if head[:6] == b"\xfd7zXZ\x00":
            return "txz"
        if head[:3] == b"BZh":
            return "tbz"
        text = head.lstrip()[:200].lower()
        if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in head[:1000].lower()):
            return "svg"
        if text.startswith((b"<!doctype html", b"<html")):
            return "html"
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "json" if text[:1] in (b"{", b"[") else "txt"

    # ---- the map of routes -------------------------------------------------------------------------------------------
    def routes_from(self, src):
        """{target: (total cost, [edges])} for every format reachable from src, cheapest route each."""
        best, heap, count = {}, [(0.0, 0, src, [])], 0
        while heap:
            cost, _n, node, path = heapq.heappop(heap)
            if node in best:
                continue
            best[node] = (cost, path)
            if node in TERMINALS or len(path) >= 4:
                continue
            for e in self.out.get(node, ()):
                if e.dst not in best:
                    count += 1
                    penalty = LOSSY_PENALTY if (path and node in LOSSY_MIDDLES) else 0.0
                    heapq.heappush(heap, (cost + e.cost + penalty, count, e.dst, path + [e]))
        best.pop(src, None)
        return best

    def label(self, node):
        return TERMINALS[node][0] if node in TERMINALS else FORMATS[node][0]

    @staticmethod
    def kind_of(node):
        return "image" if node in TERMINALS else FORMATS[node][1]

    def describe(self, src, path):
        """'Word (.docx) -> PDF -> PNG pictures, one per page', and the tools used."""
        names = [self.label(src)] + [self.label(e.dst) for e in path]
        tools = []
        for e in path:
            t = {"wordcraft": "WordCraft", "deckcraft": "DeckCraft", "gridcraft": "GridCraft", "pdftoppm": "Poppler",
                 "pdftocairo": "Poppler", "pdftotext": "Poppler", "pillow": "Pillow", "pillow-pdf": "Pillow", "pixbuf": "GTK", "table": "Inkling",
                 "pdf-to-office": "Inkling", "pdf-to-html": "Inkling", "data": "Inkling", "archive": "Inkling",
                 "ffmpeg": "ffmpeg", "pandoc": "Pandoc"}.get(e.engine, e.engine)
            if t not in tools:
                tools.append(t)
        notes = [e.note for e in path if e.note]
        return " → ".join(names), ", ".join(tools), "; ".join(dict.fromkeys(notes))

    def targets(self, src):
        """What a file of format src can sensibly be turned into: {target: (cost, path)}; empty if nothing can."""
        kind = FORMATS[src][1]
        allowed = ALLOWED[kind] | ALLOWED_EXTRA.get(src, set())
        out = {}
        for t, v in self.routes_from(src).items():
            if v[0] >= MAX_COST or self.kind_of(t) not in allowed:
                continue
            if kind == "image" and (t in TERMINALS or t in NO_TEXT_FROM_PICTURES):
                continue
            out[t] = v
        return out

    def why_not(self, fmt):
        """A sentence saying what is missing when a format can't be converted from."""
        if fmt is None:
            return "I don't recognise this kind of file."
        if fmt in AUDIO + VIDEO and not self.engines["ffmpeg"]:
            return "Audio and video need ffmpeg. Install it (Linux: sudo apt install ffmpeg; Mac: brew install ffmpeg; Windows: winget install ffmpeg)."
        kind = FORMATS[fmt][1]
        if kind in ("document", "slides", "spreadsheet") and not any(self.engines[a] for a in ("wordcraft", "deckcraft", "gridcraft")):
            return "Word, slides and spreadsheets need WordCraft, DeckCraft or GridCraft (or LibreOffice) on this computer."
        return f"Nothing on this computer converts {FORMATS[fmt][0]} files yet."

    # ---- doing it ----------------------------------------------------------------------------------------------------
    def convert(self, src, target, out_dir=None, cancel=None, hq=False, progress=None):
        """Convert one file or folder. Returns {"outputs": [paths], "route": text, "seconds": s}.
        out_dir None: next to the original. Raises ConversionError (what went wrong, for a person) or Cancelled."""
        t0 = time.monotonic()
        src = Path(src)
        fmt = self.detect(src)
        if fmt is None:
            raise ConversionError(self.why_not(None))
        routes = self.targets(fmt)
        if target not in routes:
            raise ConversionError(f"{FORMATS[fmt][0]} can't be turned into {self.label(target)} here."
                                  + (" " + self.why_not(fmt) if not routes else ""))
        path = routes[target][1]
        out_dir = Path(out_dir) if out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = self._stem(src, fmt)
        work = Path(tempfile.mkdtemp(prefix="convert-", dir=self._scratch()))
        try:
            current = self._canonical(src, fmt, work, stem)
            for i, edge in enumerate(path):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress(i, len(path), edge)
                current = self._step(edge, current, work, stem, hq)
            outputs = self._deliver(current, target, out_dir, stem)
        except (ConversionError, Cancelled):
            raise
        except subprocess.TimeoutExpired:
            raise ConversionError("this took too long (over 10 minutes)") from None
        except Exception as e:  # noqa: BLE001
            raise ConversionError(f"{type(e).__name__}: {e}") from e
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return {"outputs": outputs, "route": self.describe(fmt, path)[0], "seconds": time.monotonic() - t0}

    def convert_many(self, jobs, out_dir=None, workers=None, hq=False, cancel=None, on_result=None):
        """jobs = [(source, target)] run in parallel. on_result(index, outcome) is called as each finishes, with
        outcome = {"ok": True, **convert's result} or {"ok": False, "error": text}. Returns the outcomes in order."""
        outcomes = [None] * len(jobs)

        def one(i):
            src, target = jobs[i]
            try:
                outcome = {"ok": True, **self.convert(src, target, out_dir, cancel, hq)}
            except Cancelled:
                outcome = {"ok": False, "error": "cancelled"}
            except ConversionError as e:
                outcome = {"ok": False, "error": str(e)}
            outcomes[i] = outcome
            if on_result:
                on_result(i, outcome)
        workers = workers or max(2, min(6, os.cpu_count() or 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, range(len(jobs))))
        return outcomes

    # ---- helpers for doing it ------------------------------------------------------------------------------------------
    def _scratch(self):
        base = getattr(self.host, "WORK_DIR", None)
        try:
            Path(base).mkdir(parents=True, exist_ok=True)
            return str(base)
        except (TypeError, OSError):
            return None

    @staticmethod
    def _stem(src, fmt):
        name = src.name
        for ending in sorted(FORMATS[fmt][2], key=len, reverse=True):
            if name.lower().endswith("." + ending):
                return name[: -(len(ending) + 1)]
        return src.stem if src.suffix else name

    @staticmethod
    def _canonical(src, fmt, work, stem):
        """The source under the file ending the tools expect (a .jpeg or a file with no ending gets a copy)."""
        if fmt == "folder":
            return src
        ext = FORMATS[fmt][2][0]
        if FORMATS[fmt][1] == "archive" or src.suffix.lower().lstrip(".") == ext:
            return src
        dst = work / f"in.{ext}"
        shutil.copyfile(src, dst)
        return dst

    def _step(self, edge, src, work, stem, hq):
        """One hop of a route. Returns the file (or folder, or list of page files) it made, inside work."""
        e, dst_fmt = edge.engine, edge.dst
        out_ext = TERMINALS[dst_fmt][1] if dst_fmt in TERMINALS else (FORMATS[dst_fmt][2][0] if FORMATS[dst_fmt][2] else "")
        step_dir = Path(tempfile.mkdtemp(prefix=f"{e}-", dir=work))
        out = step_dir / f"{stem}.{out_ext}" if out_ext else step_dir / stem
        if e in ("wordcraft", "deckcraft", "gridcraft"):
            self._office(e, src, out)
        elif e == "pdftoppm":
            return self._pages(src, step_dir, stem, out_ext, hq)
        elif e == "pdftocairo":
            return self._cairo_pages(src, step_dir, stem, out_ext)
        elif e == "pdftotext":
            subprocess.run(["pdftotext", "-layout", str(src), str(out)], capture_output=True, check=True, timeout=300)
        elif e == "pdf-to-office":
            self.host.export_office(dst_fmt, src, out)
        elif e == "pdf-to-html":
            out.write_text(self._pdf_html(src, step_dir), encoding="utf-8")
        elif e == "pillow":
            self._image_to_image(src, out, dst_fmt)
        elif e == "pillow-pdf":
            self._image_to_pdf(src, out)
        elif e == "pixbuf":
            self._pixbuf_png(src, out)
        elif e == "data":
            self._data(src, out, edge.src, dst_fmt)
        elif e == "table":
            self._table(src, out, edge.src, dst_fmt)
        elif e == "archive":
            return self._archive(src, edge.src, dst_fmt, step_dir, stem)
        elif e == "ffmpeg":
            self._ffmpeg(src, out, dst_fmt)
        elif e == "pandoc":
            subprocess.run(["pandoc", str(src), "-o", str(out)], capture_output=True, check=True, timeout=300)
        else:
            raise ConversionError(f"no way to run {e}")
        if not out.exists() or (out.is_file() and out.stat().st_size == 0 and dst_fmt not in ("txt", "md", "csv", "tsv")):
            raise ConversionError(f"{e} did not produce a file")
        return out

    def _office(self, app, src, out):
        tool = self.host.find_office_tool(app)
        if tool is None:
            raise ConversionError(f"{self.host.OFFICE_APPS[app][0]} is needed for this but I can't find it ({self.host.where_to_put_apps()}).")
        r = subprocess.run([str(tool), "convert", str(src), str(out)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not out.exists():
            detail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no details"]
            raise ConversionError(f"{self.host.OFFICE_APPS[app][0]} could not convert it: {detail[0][:200]}")

    def _pages(self, pdf, step_dir, stem, ext, hq):
        """One picture per page, rendered by several processes at once for long documents."""
        dpi = 300 if hq else self.dpi
        from pypdf import PdfReader
        n = len(PdfReader(str(pdf)).pages)
        flag = {"png": ["-png"], "jpg": ["-jpeg", "-jpegopt", "quality=92"], "tiff": ["-tiff"]}[ext]
        chunks = [(1, n)] if n < 12 else [(a, min(n, a + -(-n // 4) - 1)) for a in range(1, n + 1, -(-n // 4))]

        def render(rng):
            subprocess.run(["pdftoppm", *flag, "-r", str(dpi), "-f", str(rng[0]), "-l", str(rng[1]), str(pdf), str(step_dir / "page")],
                           capture_output=True, check=True, timeout=600)
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            list(pool.map(render, chunks))
        made = "tif" if ext == "tiff" else ext                              # poppler writes .tif
        files = sorted(step_dir.glob(f"page-*.{made}"), key=lambda p: int(re.search(r"-(\d+)\.", p.name).group(1)))
        return self._number(files, step_dir, stem, ext)

    def _cairo_pages(self, pdf, step_dir, stem, ext):
        from pypdf import PdfReader
        n = len(PdfReader(str(pdf)).pages)
        flag = "-svg" if ext == "svg" else "-eps"

        def render(i):
            subprocess.run(["pdftocairo", flag, "-f", str(i), "-l", str(i), str(pdf), str(step_dir / f"page-{i:04d}.{ext}")],
                           capture_output=True, check=True, timeout=300)
        with ThreadPoolExecutor(max_workers=min(4, n)) as pool:
            list(pool.map(render, range(1, n + 1)))
        return self._number(sorted(step_dir.glob(f"page-*.{ext}")), step_dir, stem, ext)

    @staticmethod
    def _number(files, step_dir, stem, ext):
        """Name the pages stem-1.ext, stem-2.ext… (stem.ext when there is only one page)."""
        if not files:
            raise ConversionError("no pages came out")
        digits = len(str(len(files)))
        done = []
        for i, f in enumerate(files, 1):
            name = f"{stem}.{ext}" if len(files) == 1 else f"{stem}-{i:0{digits}d}.{ext}"
            target = step_dir / "result" / name
            target.parent.mkdir(exist_ok=True)
            f.replace(target)
            done.append(target)
        return done

    def _pdf_html(self, pdf, step_dir):
        """The PDF's text as one HTML file; pages without text become pictures inside it."""
        subprocess.run(["pdftoppm", "-png", "-r", "110", str(pdf), str(step_dir / "p")], capture_output=True, check=True, timeout=600)
        pngs = sorted(step_dir.glob("p-*.png"))
        page = self.host.pdf_to_html(pdf, pngs, pictures=False)
        for png in pngs:
            page = page.replace(f'src="{png.name}"', 'src="data:image/png;base64,' + base64.b64encode(png.read_bytes()).decode() + '"')
        return page

    @staticmethod
    def _image_to_image(src, out, fmt):
        from PIL import Image, ImageOps
        im = Image.open(src)
        animated = getattr(im, "is_animated", False) and fmt in ("gif", "webp")
        if not animated:
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:  # noqa: BLE001
                pass
        if fmt == "jpg":
            if im.mode in ("RGBA", "LA", "P"):                          # JPEG has no see-through: put it on white
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, "white")
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            im.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        elif fmt == "png":
            im.save(out, "PNG", optimize=True)
        elif fmt == "webp":
            im.save(out, "WEBP", quality=90, method=4, save_all=animated)
        elif fmt == "gif":
            im.save(out, "GIF", save_all=animated, optimize=True)
        elif fmt == "bmp":
            im.convert("RGBA" if im.mode == "RGBA" else "RGB").save(out, "BMP")
        elif fmt == "tiff":
            im.save(out, "TIFF", compression="tiff_lzw")
        elif fmt == "ico":
            side = min(256, im.width, im.height)
            sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side] or [(side, side)]
            im.convert("RGBA").save(out, "ICO", sizes=sizes)
        else:
            raise ConversionError(f"can't write {fmt}")

    @staticmethod
    def _image_to_pdf(src, out):
        from PIL import Image, ImageOps
        im = Image.open(src)
        frames = []
        if getattr(im, "n_frames", 1) > 1:
            from PIL import ImageSequence
            frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
        else:
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:  # noqa: BLE001
                pass
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, "white")
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            frames = [im.convert("RGB")]
        frames[0].save(out, "PDF", resolution=150.0, save_all=len(frames) > 1, append_images=frames[1:])

    @staticmethod
    def _pixbuf_png(src, out):
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        if str(src).lower().endswith(".svg"):
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(src), 2048, 2048, True)     # a drawing has no pixels: draw it big
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file(str(src))
        pb.savev(str(out), "png", [], [])

    @staticmethod
    def _data(src, out, src_fmt, dst_fmt):
        text = Path(src).read_bytes()
        if src_fmt == "json":
            value = json.loads(text)
        elif src_fmt == "yaml":
            import yaml
            value = yaml.safe_load(text)
        else:
            import tomllib
            value = tomllib.loads(text.decode("utf-8"))
        if dst_fmt == "json":
            Path(out).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        else:
            import yaml
            Path(out).write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")

    @staticmethod
    def _table(src, out, src_fmt, dst_fmt):
        """A table (CSV/TSV) <-> JSON as a list of records, the way programs expect it."""
        import csv
        text = Path(src).read_text(encoding="utf-8-sig")
        if src_fmt == "json":
            value = json.loads(text)
            if isinstance(value, dict) and all(isinstance(v, list) for v in value.values()):
                value = [dict(zip(value, row)) for row in zip(*value.values())]       # columns -> records
            if not isinstance(value, list):
                raise ConversionError("a table needs a JSON list (of records like {\"name\": …}, or of rows)")
            if value and all(isinstance(r, dict) for r in value):
                header = list(dict.fromkeys(k for r in value for k in r))
                rows = [[json.dumps(r[k], ensure_ascii=False) if isinstance(r.get(k), (dict, list)) else r.get(k, "") for k in header]
                        for r in value]
            elif all(isinstance(r, list) for r in value):
                header, rows = None, [["" if v is None else v for v in r] for r in value]
            else:
                raise ConversionError("a table needs a JSON list of records or of rows")
            with open(out, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t" if dst_fmt == "tsv" else ",")
                if header:
                    w.writerow(header)
                w.writerows(rows)
        else:
            def typed(v):
                if re.fullmatch(r"-?(0|[1-9]\d*)", v):
                    return int(v)
                if re.fullmatch(r"-?\d+\.\d+", v):
                    return float(v)
                return v
            rows = list(csv.DictReader(io.StringIO(text), delimiter="\t" if src_fmt == "tsv" else ","))
            Path(out).write_text(json.dumps([{k: typed(v) if isinstance(v, str) else v for k, v in r.items()} for r in rows],
                                            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _archive(src, src_fmt, dst_fmt, step_dir, stem):
        """Unpack (safely) and pack again in the other kind of archive; or unpack to a folder."""
        src = Path(src)
        if src_fmt == "folder":
            root, arc_root = src, src.name
        else:
            root, arc_root = step_dir / "unpacked", None
            root.mkdir()
            if src_fmt == "zip":
                with zipfile.ZipFile(src) as z:
                    for m in z.infolist():
                        target = (root / m.filename).resolve()
                        if root.resolve() not in target.parents and target != root.resolve():
                            raise ConversionError("this archive tries to write outside its folder; I did not open it")
                    z.extractall(root)
            else:
                with tarfile.open(src, "r:*") as t:
                    t.extractall(root, filter="data")
        if dst_fmt == "folder":
            kids = list(root.iterdir())
            if len(kids) == 1 and kids[0].is_dir() and src_fmt != "folder":     # a single folder inside: hand that over, not a folder in a folder
                return kids[0]
            return root
        dst = step_dir / f"{stem}.{FORMATS[dst_fmt][2][0]}"
        files = sorted(p for p in root.rglob("*") if p.is_file() or p.is_dir())
        if dst_fmt == "zip":
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                for p in files:
                    z.write(p, str(Path(arc_root or "") / p.relative_to(root)) if arc_root else str(p.relative_to(root)))
        else:
            mode = {"tar": "w", "tgz": "w:gz", "txz": "w:xz", "tbz": "w:bz2"}[dst_fmt]
            with tarfile.open(dst, mode) as t:
                for p in files:
                    t.add(p, arcname=str(Path(arc_root or "") / p.relative_to(root)) if arc_root else str(p.relative_to(root)), recursive=False)
        return dst

    @staticmethod
    def _ffmpeg(src, out, fmt):
        args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
        if fmt == "gif":
            args += ["-vf", "fps=12,scale=640:-1:flags=lanczos"]
        elif fmt == "mp3":
            args += ["-vn", "-q:a", "2"]
        elif fmt in AUDIO:
            args += ["-vn"]
        subprocess.run(args + [str(out)], capture_output=True, check=True, timeout=3600)

    # ---- putting the result where it belongs --------------------------------------------------------------------------
    @staticmethod
    def _deliver(current, target, out_dir, stem):
        """Move what the last step made out of the scratch folder: one file, a folder, or a folder of pages."""
        if isinstance(current, list):
            if len(current) == 1:
                dst = unique_path(out_dir, current[0].name)
                shutil.move(str(current[0]), str(dst))
                return [dst]
            folder = unique_path(out_dir, f"{stem} ({TERMINALS[target][1]} pages)")
            folder.mkdir()
            moved = []
            for f in current:
                shutil.move(str(f), str(folder / f.name))
                moved.append(folder / f.name)
            return moved
        current = Path(current)
        dst = unique_path(out_dir, stem if target == "folder" else current.name)
        shutil.move(str(current), str(dst))
        return [dst]
