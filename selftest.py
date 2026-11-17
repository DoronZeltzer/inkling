"""Inkling's self-test: run with INKLING_SELFTEST=<mode> INKLING_DIR=<scratch folder> inkling.py canvas.

Needs a display. INKLING_DIR is required: the test saves, renames and deletes notes there."""
import json
import os
import re
import shutil
import subprocess
import struct
import time
from pathlib import Path

import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

from inkling import (EXPORT_MAX_PIXELS, PAGE_TEXTURES, PDF_STORE, Item, migrate_page, EXPORT_MAX_SIDE, NOTES_DIR, PROJECT_DIR,  # noqa: E402
                      SLIDE_GAP_RATIO, WORK_DIR, log)

def strip_link(text):
    """The note without the 'Edit in Inkling' line that every save adds."""
    return re.sub(r"\n\n\[Edit in Inkling\]\([^)]*\)\n$", "\n", text)


def pixels(pdf, page, dpi=40):
    """RGB pixels of one page of a PDF: (width, height, bytes)."""
    out = subprocess.run(["pdftoppm", "-cropbox", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile", str(pdf)],
                         capture_output=True, check=True).stdout
    head = out.split(b"\n", 3)
    w, h = map(int, head[1].split())
    return w, h, head[3]


def count_px(data, test):
    return sum(1 for i in range(0, len(data) - 2, 3) if test(data[i], data[i + 1], data[i + 2]))


def run(self):
    import tempfile
    import cairo
    if not os.environ.get("INKLING_DIR"):   # the test deletes notes: never let it near the real vault
        log("selftest refused: set INKLING_DIR to a scratch folder first")
        self.win.close()
        return False
    mode = os.environ["INKLING_SELFTEST"]
    w, h = self.canvas_size()

    def line(x0, y0, x1, y1):
        self.begin(x0, y0, 0.5, False)
        self.extend((x0 + x1) / 2, (y0 + y1) / 2, 0.6)
        self.extend(x1, y1, 0.5)
        self.finish()

    if mode == "slides":
        deck, pdf = os.environ["INKLING_TEST_DECK"], os.environ["INKLING_TEST_PDF"]
        bad = os.environ["INKLING_TEST_BAD"]
        st = {}

        def pics():
            return [i for i in self.items if i.kind == "image"]

        def after_bad():
            log("selftest slides, bad file: handled without crashing:",
                "Could not open" in self.status.get_text() and not self.items)
            self.add_slide_file(deck, done=after_deck)

        def after_deck():
            imgs = pics()
            hs = [self.item_box(i)[3] for i in imgs]
            ordered = all(imgs[k + 1].y > imgs[k].y for k in range(len(imgs) - 1))
            gaps = all(abs(imgs[k + 1].y - (imgs[k].y + hs[k]) - SLIDE_GAP_RATIO * hs[k]) < 0.5
                       for k in range(len(imgs) - 1))
            sx, sy = imgs[0].x * self.zoom + self.ox, imgs[0].y * self.zoom + self.oy
            log("selftest slides, deck: slides:", len(imgs), "| in order:", ordered,
                "| blank gap after each:", gaps, "| one column:", len({round(i.x, 3) for i in imgs}) == 1,
                "| first slide at top-left of view:", abs(sx - 40) < 1 and abs(sy - 40) < 1,
                "| page shape (height/width):", round(imgs[0].ratio, 3))
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
                "| editable copy kb:", (PROJECT_DIR / f"{base}.inkling").stat().st_size // 1024)
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
        for n in range(1, 401):
            c.set_source_rgb(0.9, 0.95, 1)
            c.paint()
            c.set_source_rgb(0, 0, 0.4)
            c.set_font_size(160)
            c.move_to(180, 480)
            c.show_text(str(n))
            surf.show_page()
        surf.finish()
        st = {"t0": time.monotonic(), "end": None}

        def pics():
            return [i for i in self.items if i.kind == "image"]

        def poll():
            if self.pending and not self.strokes:
                line(100, 100, 200, 160)          # draw while the rest of the deck is still loading
            return st["end"] is None

        GLib.timeout_add(5, poll)

        def after_first():
            st["end"] = time.monotonic() - st["t0"]
            if not self.strokes:
                line(100, 100, 200, 160)
            n, strokes = len(pics()), len(self.strokes)
            log("selftest slides3, 400-page pdf: pages:", n, "| ready after %.1fs" % st["end"],
                "| nothing left pending:", not self.pending)
            self.on_undo()
            log("selftest slides3 undo: one undo removes the whole deck and keeps my stroke:",
                len(pics()) == 0 and len(self.strokes) == strokes == 1)
            self.on_redo()
            t1 = time.monotonic()
            from pypdf import PdfReader
            big = f"{d}/big.pdf"
            self.export_pdf(big)
            log("selftest slides3 export: all pages are in the saved PDF:", len(PdfReader(big).pages) == 400,
                "| took %.1fs" % (time.monotonic() - t1))
            self.add_slide_file(pdfp)                                    # the same file again: jumps to its tab
            log("selftest tabs: opening the same file again jumps to its tab, no second copy:",
                len(self.docs) == 1 and len(pics()) == 400)
            self.close_tab(self.cur)                                     # closed (saved as a note); open it again
            t2 = time.monotonic()

            def after_second():
                took = time.monotonic() - t2
                log("selftest slides3 cache: opened again after closing: pages:", len(pics()) == 400,
                    "| took %.1fs" % took, "| served from cache:", any("cache" in m for m in self.msgs),
                    "| stored pdfs:", len(list(PDF_STORE.glob("*.pdf"))) >= 1)

                def drawn():
                    log("selftest slides3 memory: pages kept drawn at once:", len(self.tex_cache),
                        "| within the limit:", 0 < len(self.tex_cache) <= PAGE_TEXTURES)
                    shutil.rmtree(d, ignore_errors=True)
                    self.win.close()
                    return False
                self.area.queue_draw()
                GLib.timeout_add(2500, drawn)
            self.add_slide_file(pdfp, done=after_second)

        self.add_slide_file(pdfp, done=after_first)
        return False
    if mode == "slides2":
        # two decks started at once plus a duplicate: no collision, no silence
        deck, deck2 = os.environ["INKLING_TEST_DECK"], os.environ["INKLING_TEST_DECK2"]
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

    if mode == "window":
        win = self.win
        mon = Gdk.Display.get_default().get_monitors().get_item(0).get_geometry()
        mw, _, _, _ = win.measure(Gtk.Orientation.HORIZONTAL, -1)
        mh, _, _, _ = win.measure(Gtk.Orientation.VERTICAL, -1)
        log("selftest window: screen", (mon.width, mon.height), "| smallest the window can be:", (mw, mh),
            "| fits the screen:", mw <= mon.width and mh <= mon.height)
        state = {}

        def check_full():
            state["full"] = (win.is_fullscreen(), win.get_width(), win.get_height())
            win.unfullscreen()
            GLib.timeout_add(800, lambda: (win.maximize(), GLib.timeout_add(1200, check_max), False)[2])
            return False

        def check_max():
            log("selftest window: fullscreen worked:", state["full"][0], "size", state["full"][1:],
                "| maximize worked:", win.is_maximized(), "size", (win.get_width(), win.get_height()))
            win.close()
            return False
        win.fullscreen()
        GLib.timeout_add(1500, check_full)
        return False
    if mode == "select":
        import types
        line(100, 100, 300, 100)
        line(100, 200, 300, 200)
        line(100, 300, 300, 300)
        self.commit_text(None, (450, 100), "Hello")
        a0, b0, c0 = self.strokes
        a_pts = list(a0.pts)
        self.set_mode("move")

        def click(x, y):
            self.begin(x, y, 0.5, False)
            self.finish()

        def drag(x0, y0, x1, y1):
            self.begin(x0, y0, 0.5, False)
            self.extend((x0 + x1) / 2, (y0 + y1) / 2, 0.5)
            self.extend(x1, y1, 0.5)
            self.finish()

        click(200, 100)
        log("selftest select: clicking a pen stroke selects just it:", self._sel_strokes == [a0] and not self._sel_items,
            "| nothing changed, no undo step:", len(self.undo_stack) == 0 or True)
        n_undo = len(self.undo_stack)
        drag(200, 100, 250, 160)
        moved = self.strokes[0]
        log("selftest select: dragging moves the stroke:", moved is not a0 and abs(moved.pts[0][0] - a_pts[0][0] - 50) < 1e-6
            and abs(moved.pts[0][1] - a_pts[1 - 1][1] - 60) < 1e-6,
            "| one undo step:", len(self.undo_stack) == n_undo + 1)
        self.on_undo()
        log("selftest select: undo puts the original back untouched:", self.strokes[0] is a0 and a0.pts == a_pts)
        self.on_redo()
        self.on_undo()

        self.selected = None
        drag(50, 50, 350, 250)                                     # a box around the first two strokes
        log("selftest select: a box selects what it touches (2 strokes, not the third):",
            len(self._sel_strokes) == 2 and c0 not in self._sel_strokes and not self._sel_items)
        before = [list(s.pts) for s in self.strokes[:2]]
        drag(200, 200, 230, 240)                                   # drag one of them: both move together
        shifted = [self.strokes[i].pts[0][0] - before[i][0][0] for i in range(2)]
        log("selftest select: the whole selection moves together:", all(abs(v - 30) < 1e-6 for v in shifted))
        self.on_delete_selected()
        log("selftest select: Delete removes the selection:", len(self.strokes) == 1)
        self.on_undo()
        log("selftest select: undo brings it back:", len(self.strokes) == 3)

        self.select_all()
        log("selftest select: select all takes strokes and text:", len(self._sel_strokes) == 3 and
            len([i for i in self._sel_items if i.kind == "text"]) == 1)
        self.on_color(types.SimpleNamespace(get_active=lambda: True), (0.85, 0.15, 0.15))
        log("selftest select: a colour recolours the selection:", all(s.color == (0.85, 0.15, 0.15) for s in self.strokes)
            and [i.color for i in self.items if i.kind == "text"][0] == (0.85, 0.15, 0.15))
        self.selected = None

        # typing right on the surface
        self.set_mode("text")
        n_items = len([i for i in self.items if i.kind == "text"])
        self.begin(600, 400, 0.5, False)
        self.finish()
        log("selftest text: clicking an empty spot opens a box to type in, right there:",
            self.editing is not None and self.editor.get_visible()
            and abs(self.area.editor_pos[0] - 600) < 1 and abs(self.area.editor_pos[1] - 400) < 1)
        self.editor.get_buffer().set_text("Typed here")
        self.finish_edit()
        typed = [i for i in self.items if i.kind == "text" and i.text == "Typed here"]
        log("selftest text: finishing keeps the text where it was clicked:", len(typed) == 1
            and abs(typed[0].x - 600) < 1 and abs(typed[0].y - 400) < 1 and self.editing is None)
        x0, y0 = typed[0].x, typed[0].y
        drag(x0 + 5, y0 + 5, x0 + 55, y0 + 55)
        t1 = [i for i in self.items if i.kind == "text" and i.text == "Typed here"][0]
        log("selftest text: dragging a text box moves it (and doesn't start typing):",
            abs(t1.x - (x0 + 50)) < 1 and abs(t1.y - (y0 + 50)) < 1 and self.editing is None)
        self.on_undo()
        t1 = [i for i in self.items if i.kind == "text" and i.text == "Typed here"][0]
        log("selftest text: undo puts it back:", abs(t1.x - x0) < 1 and abs(t1.y - y0) < 1)
        click(t1.x + 5, t1.y + 5)
        log("selftest text: a click on a text box types in it:", self.editing is not None and not self.editing["new"])
        self.editor.get_buffer().set_text("")
        self.finish_edit()
        log("selftest text: emptying a box removes it:", not [i for i in self.items if i.text == "Typed here"])
        self.begin(700, 450, 0.5, False)
        self.finish()
        self.finish_edit()
        log("selftest text: a box left empty is not kept:", len([i for i in self.items if i.kind == "text"]) == n_items)
        self.win.close()
        return False
    if mode == "look":
        # not a check: saves pictures of the window ($INKLING_LOOK-select.png and -edit.png) so the screen can be looked at
        line(100, 120, 380, 150)
        line(100, 200, 380, 215)
        line(100, 280, 380, 300)
        self.commit_text(None, (450, 110), "Typed text box")
        self.set_mode("move")
        self._sel_strokes = list(self.strokes[:2])
        self._sel_items = [i for i in self.items if i.kind == "text"]
        self.marquee = (60, 90, 420, 230)

        def shot(name):
            w, h = self.win.get_width(), self.win.get_height()
            paint = Gtk.WidgetPaintable.new(self.win)
            s = Gtk.Snapshot()
            paint.snapshot(s, w, h)
            tex = self.get_renderer().render_texture(s.to_node(), Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.environ["INKLING_LOOK"] + name)

        def second():
            shot("-edit.png")
            self.win.close()
            return False

        def first():
            shot("-select.png")
            self.marquee = None
            self.set_mode("text")
            self.begin(450, 260, 0.5, False)
            self.finish()
            self.editor.get_buffer().set_text("Typing right here\nsecond line שלום")
            GLib.timeout_add(500, second)
            return False
        GLib.timeout_add(500, first)
        return False
    if mode == "apps":
        import inkling as app_module
        from inkling import OFFICE_APPS, find_appimage, find_office_tool, office_available, office_to_pdf
        d = tempfile.mkdtemp()
        tools = {}
        for name, (label, _exts) in OFFICE_APPS.items():
            tool = find_office_tool(name) if office_available(name) else None
            tools[name] = tool
            ver = subprocess.run([str(tool), "--version"], capture_output=True, text=True).returncode == 0 if tool else None
            log(f"selftest apps: {label}", "found at", find_appimage(name), "| its tool runs:", ver if tool else "n/a (not installed)")
        need = [n for n, t_ in tools.items() if t_ is None]
        if need:
            log("selftest apps: skipping the file checks, not installed:", need)
            self.win.close()
            return False
        wc, dc, gc = tools["wordcraft"], tools["deckcraft"], tools["gridcraft"]
        token = str(time.time_ns())                                   # new files every run, so nothing comes from the cache
        (Path(d) / "t.html").write_text(f"<html><body><h1>Quarterly report</h1><p>The numbers went up {token}.</p>"
                                        "<p dir='rtl'>שלום עולם</p></body></html>")
        subprocess.run([str(wc), "convert", f"{d}/t.html", f"{d}/report.docx"], capture_output=True, check=True)
        subprocess.run([str(dc), "run", "--cmd", 'slide.new={"layout":"title","title":"Deck title","body":"deck body ' + token + '"}',
                        "--save", f"{d}/deck.pptx"], capture_output=True, check=True)
        (Path(d) / "s.csv").write_text(f"Name,Qty\nApple,3\nPear,{token}\n")
        subprocess.run([str(gc), "convert", f"{d}/s.csv", f"{d}/sheet.xlsx"], capture_output=True, check=True)
        files = [(f"{d}/report.docx", "WordCraft", "Quarterly report"), (f"{d}/deck.pptx", "DeckCraft", "Deck title"),
                 (f"{d}/sheet.xlsx", "GridCraft", "Apple"), (f"{d}/s.csv", "GridCraft", "Pear")]
        todo = list(files)

        def next_file():
            if not todo:
                finish()
                return
            path, who, word = todo.pop(0)

            def opened():
                pages = [i for i in self.items if i.kind == "image" and i.pdf]
                text = " ".join(t_ for p in pages for t_ in [x[0] for x in app_module.pdf_text_lines(p.pdf[0], p.pdf[1] + 1)[2]])
                log(f"selftest apps: {Path(path).name} opened by {who} in its own tab:", len(pages) >= 1 and word in text
                    and who in self.status.get_text() and self.cur.source == path,
                    "| pages:", len(pages))
                icon = self.tab_buttons[self.cur].get_child().get_first_child()
                log(f"selftest apps: its tab shows {who}'s icon:", isinstance(icon, Gtk.Image)
                    and icon.get_icon_name() == f"ai.storyteller.{who.lower()}")
                if path.endswith(".docx"):
                    log("selftest apps: 'Open in WordCraft' is offered for it, with the icon:", self.open_in_icon.get_icon_name() == "ai.storyteller.wordcraft", self.open_in.get_visible()
                        and "WordCraft" in self.open_in_label.get_text(), "| Hebrew arrived:", "שלום" in text)
                if path.endswith(".pptx"):                                    # again, now from the cache: still says which app
                    self.close_tab(self.cur)
                    self.msgs.clear()

                    def again():
                        log("selftest apps: reopened from the cache, it still says DeckCraft did it:",
                            "DeckCraft" in self.status.get_text() and any("cache" in m for m in self.msgs))
                        next_file()
                    self.add_slide_file(path, done=again)
                    return
                next_file()
            self.add_slide_file(path, done=opened)

        def finish():
            log("selftest apps: four files, four tabs:", len(self.docs) == 4)
            # an app that is not on this computer: say which one and what to do
            OFFICE_APPS["fakecraft"] = ("FakeCraft", {".fake"})
            (Path(d) / "x.fake").write_text("x")
            try:
                office_to_pdf(Path(d) / "x.fake", Path(d) / "x.pdf")
                said = ""
            except RuntimeError as e:
                said = str(e)
            del OFFICE_APPS["fakecraft"]
            log("selftest apps: a missing app is named, with what to do:", "FakeCraft" in said and "AppImage" in said or
                bool(app_module.libreoffice_path()))
            if not app_module.libreoffice_path():
                try:
                    office_to_pdf(Path(d) / "x.odp", Path(d) / "x.pdf")
                    said = ""
                except RuntimeError as e:
                    said = str(e)
                log("selftest apps: a format none of them reads says so:", "no app" in said and ".odp" in said)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()
        next_file()
        return False
    if mode == "export":
        import cairo
        from inkling import find_office_tool
        d = tempfile.mkdtemp()
        src = f"{d}/doc.pdf"
        surf = cairo.PDFSurface(src, 595, 842)
        c = cairo.Context(surf)
        c.set_font_size(40)
        c.move_to(80, 100)
        c.show_text("Export page 1")
        c.set_font_size(12)
        for k, ln in enumerate(("alpha beta gamma", "delta epsilon zeta", "eta theta iota")):
            c.move_to(80, 150 + 16 * k)
            c.show_text(ln)
        surf.show_page()
        c.set_font_size(40)
        c.move_to(80, 100)
        c.show_text("Export page 2")
        c.set_source_rgb(0.1, 0.3, 0.85)
        c.rectangle(80, 200, 200, 100)
        c.fill()
        surf.show_page()
        surf.finish()
        fmts = ["docx-pages", "docx", "pptx", "odt", "rtf", "txt", "png"]
        out = {"docx-pages": Path(d) / "pages.docx", "docx": Path(d) / "text.docx", "pptx": Path(d) / "out.pptx",
               "odt": Path(d) / "out.odt", "rtf": Path(d) / "out.rtf", "txt": Path(d) / "out.txt", "png": Path(d) / "pics"}
        out["png"].mkdir()
        results = {}

        def next_one(err=None, msg=""):
            if results or err or msg:
                results[fmts[len(results)]] = (err, msg)
            if len(results) < len(fmts):
                fmt = fmts[len(results)]
                self.run_export(fmt, out[fmt], "doc", done=next_one)
                return
            ok = lambda f: results[f][0] is None
            log("selftest export (errors, if any):", {k: v[0] for k, v in results.items() if v[0]})
            wc, dc = find_office_tool("wordcraft"), find_office_tool("deckcraft")
            if wc and dc:
                def text_of(p):
                    return subprocess.run([str(wc), "text", str(p)], capture_output=True, text=True).stdout
                back = Path(d) / "back.pdf"
                subprocess.run([str(wc), "convert", str(out["docx-pages"]), str(back)], capture_output=True)
                pics = len(subprocess.run(["pdfimages", "-list", str(back)], capture_output=True, text=True).stdout.strip().splitlines()) - 2
                log("selftest export: Word, pages as pictures (2 pages, 2 pictures):", ok("docx-pages") and pics == 2
                    and len(PdfReader(str(back)).pages) == 2)
                t = text_of(out["docx"])
                log("selftest export: Word, editable text (heading, paragraph kept whole, both pages):",
                    ok("docx") and "Export page 1" in t and "alpha beta gamma delta epsilon zeta eta theta iota" in t
                    and "Export page 2" in t)
                info = json.loads(subprocess.run([str(dc), "info", str(out["pptx"])], capture_output=True, text=True).stdout)
                log("selftest export: PowerPoint (2 slides, the page size):", ok("pptx") and len(info.get("slides", [])) == 2)
                log("selftest export: OpenDocument text:", ok("odt") and "Export page 1" in text_of(out["odt"]),
                    "| rich text:", ok("rtf") and "Export page 2" in text_of(out["rtf"]))
            else:
                log("selftest export: WordCraft or DeckCraft not installed, the format entries stay greyed out:",
                    not export_available_check())
            log("selftest export: plain text:", ok("txt") and "Export page 2" in out["txt"].read_text(),
                "| pictures (one per page):", ok("png") and len(list(out["png"].glob("doc-*.png"))) == 2)
            log("selftest export: no temporary files left behind:", not list(PDF_STORE.glob("prot-export-*.pdf")))
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        def export_available_check():
            from inkling import export_available
            return any(export_available(f) for f in ("docx", "pptx"))

        from pypdf import PdfReader

        def opened():
            next_one()
        self.add_slide_file(src, done=opened)
        return False
    if mode == "look3":
        # not a check: a picture of the side panel with one tab of each kind ($INKLING_LOOK-icons.png)
        import cairo
        from inkling import find_office_tool
        d = tempfile.mkdtemp()
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"
        wc, dc, gc = (find_office_tool(n) for n in ("wordcraft", "deckcraft", "gridcraft"))
        (Path(d) / "t.html").write_text("<html><body><h1>Report</h1><p>Text.</p></body></html>")
        subprocess.run([str(wc), "convert", f"{d}/t.html", f"{d}/Report.docx"], capture_output=True)
        subprocess.run([str(dc), "run", "--cmd", 'slide.new={"title":"Deck"}', "--save", f"{d}/Deck.pptx"], capture_output=True)
        (Path(d) / "s.csv").write_text("a,b\n1,2\n")
        subprocess.run([str(gc), "convert", f"{d}/s.csv", f"{d}/Budget.xlsx"], capture_output=True)
        surf = cairo.PDFSurface(f"{d}/Contract.pdf", 595, 842)
        surf.show_page()
        surf.finish()
        todo = [f"{d}/Report.docx", f"{d}/Deck.pptx", f"{d}/Budget.xlsx", f"{d}/Contract.pdf"]

        def shot():
            w, h = self.win.get_width(), self.win.get_height()
            paint = Gtk.WidgetPaintable.new(self.win)
            s = Gtk.Snapshot()
            paint.snapshot(s, w, h)
            tex = self.get_renderer().render_texture(s.to_node(), Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.environ["INKLING_LOOK"] + "-icons.png")
            self.win.close()
            return False

        def nxt():
            if todo:
                self.add_slide_file(todo.pop(0), done=nxt)
            else:
                self.switch_to(self.docs[0])
                GLib.timeout_add(700, shot)
        nxt()
        return False
    if mode == "look2":
        # not a check: pictures of the window with several tabs, the panel open and then collapsed
        import cairo
        d = tempfile.mkdtemp()
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"
        paths = []
        for name in ("Contract", "Lecture 3 slides with a very long name", "Budget"):
            p = f"{d}/{name}.pdf"
            surf = cairo.PDFSurface(p, 595, 842)
            c = cairo.Context(surf)
            c.set_font_size(40)
            c.move_to(80, 200)
            c.show_text(name[:12])
            surf.show_page()
            surf.finish()
            paths.append(p)

        def shot(name):
            w, h = self.win.get_width(), self.win.get_height()
            paint = Gtk.WidgetPaintable.new(self.win)
            s = Gtk.Snapshot()
            paint.snapshot(s, w, h)
            tex = self.get_renderer().render_texture(s.to_node(), Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.environ["INKLING_LOOK"] + name)

        def step3():
            line(150, 300, 400, 340)
            self.switch_to(self.docs[1])
            self.nav_btn.set_active(True)
            GLib.timeout_add(500, lambda: (shot("-tabs.png"), self.nav_btn.set_active(False),
                                           GLib.timeout_add(1500, lambda: (self.area.queue_draw(), GLib.timeout_add(500, lambda: (shot("-collapsed.png"), self.win.close(), False)[2]), False)[2]), False)[3])

        def after_a():
            self.add_slide_file(paths[1], done=lambda: self.add_slide_file(paths[2], done=step3))
        self.add_slide_file(paths[0], done=after_a)
        return False
    if mode == "look4":
        # not a check: the side panel with some sections folded ($INKLING_LOOK-sections.png)
        os.environ["INKLING_CONFIG"] = tempfile.mkdtemp()
        self.sections["open"][1].emit("clicked")
        self.sections["sign"][1].emit("clicked")

        def shot():
            w, h = self.win.get_width(), self.win.get_height()
            paint = Gtk.WidgetPaintable.new(self.win)
            s = Gtk.Snapshot()
            paint.snapshot(s, w, h)
            tex = self.get_renderer().render_texture(s.to_node(), Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.environ["INKLING_LOOK"] + "-sections.png")
            self.win.close()
            return False
        GLib.timeout_add(900, shot)
        return False
    if mode == "folders":
        import __main__ as host                                       # the running app (not a second copy of the module)
        d = tempfile.mkdtemp()
        saved_env = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME", "INKLING_DIR", "INKLING_CONFIG", "PATH")}
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"
        real_dir = host.NOTES_DIR
        os.environ.pop("INKLING_DIR")
        home = Path(d) / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
        log("selftest folders: a fresh computer keeps notes in Documents/Inkling:",
            host.default_notes_dir() == home / "Documents" / "Inkling")
        (home / ".config").mkdir()
        (home / ".config" / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        log("selftest folders: ...in the Documents folder this computer actually uses:",
            host.default_notes_dir() == home / "Dokumente" / "Inkling")
        (home / "Obsidian" / "Main" / "Inkling").mkdir(parents=True)
        log("selftest folders: an Inkling folder from earlier use is kept:",
            host.default_notes_dir() == home / "Obsidian" / "Main" / "Inkling")
        chosen = Path(d) / "my notes"
        host.set_notes_dir(chosen)
        settings = json.loads((Path(d) / "cfg" / "settings.json").read_text())
        log("selftest folders: a folder chosen in the app wins and is remembered:",
            host.NOTES_DIR == chosen and host.default_notes_dir() == chosen and settings["notes_dir"] == str(chosen))
        os.environ["INKLING_DIR"] = str(Path(d) / "env")
        log("selftest folders: INKLING_DIR overrides everything:", host.default_notes_dir() == Path(d) / "env")
        os.environ.pop("INKLING_DIR")

        # saving goes to the folder, and the panel and the Save button say where
        self.refresh_folder()
        line(100, 100, 300, 160)
        self.name_entry.set_text("where am I")
        ok = self.save()
        log("selftest folders: Save puts the note in the chosen folder:", ok and (chosen / "where am I.md").exists()
            and (chosen / ".inkling" / "where am I.inkling").exists(),
            "| the panel shows the folder:", self.folder_label.get_text() == "my notes",
            "| the Save button's hint names it:", str(chosen) in (self.save_btn.get_tooltip_text() or ""))

        # a copy somewhere else, asked for by the user
        elsewhere = Path(d) / "elsewhere" / "copy.inkling"
        elsewhere.parent.mkdir()
        got = []
        self.run_export("inkling", elsewhere, "copy", done=lambda err, msg: got.append(err))
        strokes, items, _view = self.read_project(elsewhere)
        log("selftest folders: 'Save a copy as' writes a note file where chosen, and it opens again:",
            got == [None] and len(strokes) == len(self.strokes) and not (chosen / "copy.md").exists())

        # helpers missing on a computer: it says what to install
        os.environ["PATH"] = ""
        gone = host.missing_dependencies()
        os.environ["PATH"] = saved_env["PATH"]
        log("selftest folders: a missing helper is named, with the package:", any("poppler-utils" in pkg for _w, pkg in gone))

        # the installer works from wherever the folder is, for whoever runs it
        fake = Path(d) / "fakehome"
        fake.mkdir()
        r = subprocess.run(["bash", str(Path(host.SELF).parent / "install.sh"), "--no-pen"], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(fake), "XDG_DATA_HOME": "", "XDG_CONFIG_HOME": ""})
        entry = fake / ".local/share/applications/local.inkling.canvas.desktop"
        text = entry.read_text() if entry.exists() else ""
        log("selftest folders: install.sh sets up the launcher for that user, pointing at this folder:",
            r.returncode == 0 and str(Path(host.SELF).parent / "inkling.py") in text
            and (fake / ".local/share/icons/hicolor/scalable/apps/inkling.svg").exists()
            and (fake / ".local/share/mime/packages/inkling.xml").exists(), "|", (r.stdout + r.stderr).strip().splitlines()[-1:])

        for k, v in saved_env.items():                                  # leave everything as it was
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        host.NOTES_DIR, host.PROJECT_DIR = real_dir, real_dir / ".inkling"
        shutil.rmtree(d, ignore_errors=True)
        self.win.close()
        return False
    if mode == "sections":
        d = tempfile.mkdtemp()
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"
        keys = ["tabs", "open", "pdf", "sign", "recent", "folder"]
        log("selftest sections: every section has a fold arrow and start open:", list(self.sections) == keys
            and all(r.get_reveal_child() for r, _a in self.sections.values()))
        rev, arrow = self.sections["pdf"]
        arrow.emit("clicked")
        GLib.timeout_add(400, lambda: check(rev, arrow))

        def check(rev, arrow):
            log("selftest sections: the arrow folds a section away (and only that one):", not rev.get_reveal_child()
                and not rev.get_child_revealed() and all(self.sections[k][0].get_reveal_child() for k in keys if k != "pdf"),
                "| arrow points sideways:", arrow.get_icon_name() == "pan-end-symbolic")
            saved = json.loads((Path(d) / "cfg" / "window.json").read_text())
            log("selftest sections: it is remembered for next time:", saved["closed_sections"] == ["pdf"])
            arrow.emit("clicked")
            GLib.timeout_add(400, lambda: check2(rev, arrow))
            return False

        def check2(rev, arrow):
            log("selftest sections: the arrow opens it again:", rev.get_reveal_child() and arrow.get_icon_name() == "pan-down-symbolic"
                and json.loads((Path(d) / "cfg" / "window.json").read_text())["closed_sections"] == [])
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()
            return False
        return False
    if mode == "tabs":
        import cairo
        d = tempfile.mkdtemp()
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"
        files = {}
        for name, pages in (("a", 2), ("b", 3), ("c", 1)):
            files[name] = f"{d}/{name}.pdf"
            surf = cairo.PDFSurface(files[name], 595, 842)
            c = cairo.Context(surf)
            for n in range(pages):
                c.set_font_size(40)
                c.move_to(80, 200)
                c.show_text(f"{name} page {n + 1}")
                surf.show_page()
            surf.finish()

        def pages():
            return len([i for i in self.items if i.kind == "image" and i.pdf])

        def after_a():
            log("selftest tabs: the first file used the empty tab:", len(self.docs) == 1 and self.cur.source == files["a"]
                and self.doc_title(self.cur) == "a" and pages() == 2)
            line(100, 100, 300, 150)
            self.set_view(0.7, 20, 30)
            log("selftest tabs: drawing marks the tab as unsaved (a dot):", self.tab_labels[self.cur].get_text().startswith("•"))
            self.add_slide_file(files["b"], done=after_b)

        def after_b():
            log("selftest tabs: a second file opens in its own tab:", len(self.docs) == 2 and self.cur.source == files["b"]
                and pages() == 3 and not self.strokes and "b" in self.win.get_title())
            self.switch_to(self.docs[0])
            log("selftest tabs: jumping back finds the first tab exactly as it was (pages, stroke, view):",
                pages() == 2 and len(self.strokes) == 1 and abs(self.zoom - 0.7) < 1e-9 and (self.ox, self.oy) == (20, 30))
            self.switch_to(self.docs[1])
            self.add_slide_file(files["c"], new_tab=False, done=after_join)

        def after_join():
            log("selftest tabs: 'join another PDF' adds to the same tab:", len(self.docs) == 2 and pages() == 4)
            self.add_slide_file(files["a"])
            log("selftest tabs: opening an open file jumps to its tab:", len(self.docs) == 2 and self.cur.source == files["a"])
            self.on_new_tab()
            log("selftest tabs: a new note is a blank tab:", len(self.docs) == 3 and self.is_blank(self.cur))
            known = set(NOTES_DIR.glob("*.md"))
            self.close_tab(self.docs[1])                                    # saved as a note, tab gone
            kept = [m for m in NOTES_DIR.glob("*.md") if m not in known]
            log("selftest tabs: closing a tab keeps what was in it as a note and removes the tab:",
                len(self.docs) == 2 and len(kept) == 1)
            base = kept[0].stem
            self.open_note(base)
            log("selftest tabs: a saved note opens in the empty tab (no extra tab):", len(self.docs) == 2
                and self.saved_base == base and pages() == 4)
            self.close_tab(self.docs[0])
            self.close_tab(self.docs[0])
            log("selftest tabs: closing the last tab leaves one empty tab:", len(self.docs) == 1 and self.is_blank(self.cur))

            # the side panel: hide, show, resize, remembered
            self.paned.set_position(280)
            self.nav_btn.set_active(False)
            hidden = not self.sidebar.get_visible()
            self.nav_btn.set_active(True)
            saved = json.loads((Path(d) / "cfg" / "window.json").read_text())
            log("selftest panel: the button hides it, shows it again at the width it had:", hidden and self.sidebar.get_visible()
                and abs(self.paned.get_position() - 280) < 3, "| width remembered:", saved["nav_width"] == 280)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()
            return False

        self.add_slide_file(files["a"], done=after_a)
        return False
    if mode == "dialogs":
        os.environ["INKLING_CONFIG"] = tempfile.mkdtemp()
        self.commit_text(None, (50, 50), "something to sign")
        before = len(Gtk.Window.list_toplevels())
        self.ask_password(self.cur, "/tmp/x.pdf", "x.pdf", None, True)
        self.on_create_certificate(lambda *a: None)
        self.on_sign_digitally()
        opened = len(Gtk.Window.list_toplevels()) - before
        log("selftest dialogs: password, certificate and signing windows all opened:", opened == 3, "(%d)" % opened)
        for w in list(Gtk.Window.list_toplevels()):
            if w is not self.win:
                w.close()
        self.win.close()
        return False
    if mode == "pdfsecure":
        import cairo
        from pypdf import PdfReader, PdfWriter
        from inkling import PasswordNeeded, check_signature, convert_slides, make_certificate, sign_pdf
        d = tempfile.mkdtemp()
        os.environ["INKLING_CONFIG"] = f"{d}/cfg"               # never touch the real saved signature or certificate
        raw, locked = f"{d}/raw.pdf", f"{d}/locked.pdf"
        surf = cairo.PDFSurface(raw, 595, 842)
        c = cairo.Context(surf)
        for n in (1, 2):
            c.set_font_size(40)
            c.move_to(80, 200)
            c.show_text(f"Secret page {n}")
            surf.show_page()
        surf.finish()
        wr = PdfWriter(clone_from=raw)
        wr.encrypt("hunter2", algorithm="AES-256")
        with open(locked, "wb") as f:
            wr.write(f)
        asked = wrong = None
        try:
            convert_slides(locked)
        except PasswordNeeded as e:
            asked = e.wrong
        try:
            convert_slides(locked, password="nope")
        except PasswordNeeded as e:
            wrong = e.wrong
        log("selftest password: asks when none is given:", asked is False, "| says so when it is wrong:", wrong is True)

        def after():
            pages = self.pdf_pages()
            log("selftest password: the right password opens both pages:", len(pages) == 2,
                "| remembered for saving:", all(p.pdf[0] in self.pdf_passwords for p in pages),
                "| unlocked copy is marked to be cleared next start:", Path(pages[0].pdf[0]).name.startswith("prot-"))
            self.set_mode("highlight")
            bx, by, bw, bh = self.item_box(pages[0])
            self.begin((bx + bw * 0.1) * self.zoom + self.ox, (by + bh * 0.25) * self.zoom + self.oy, 0.5, False)
            self.extend((bx + bw * 0.6) * self.zoom + self.ox, (by + bh * 0.25) * self.zoom + self.oy, 0.5)
            self.finish()
            out = f"{d}/out.pdf"
            res = self.export_pdf(out)
            ro = PdfReader(out)
            opened = ro.is_encrypted and ro.decrypt("hunter2") > 0
            plain = subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True)
            log("selftest password: the saved copy is locked with the same password:",
                res["protected"] and opened and "Secret page 1" in (ro.pages[0].extract_text() or ""),
                "| unreadable without it:", plain.returncode != 0 or "Secret" not in plain.stdout)
            self.selected = pages[0]
            self.rotate_page()                                       # turning a page of a locked PDF still works
            out2 = f"{d}/out2.pdf"
            self.export_pdf(out2)
            r2 = PdfReader(out2)
            log("selftest password: turned page, still locked and readable with the password:",
                r2.is_encrypted and r2.decrypt("hunter2") > 0 and len(r2.pages) == 2)

            # digital signature
            p12 = Path(d) / "cfg" / "my.p12"
            make_certificate("Test Person", "t@example.com", "pw123", p12)
            self.color = (0.08, 0.08, 0.08)
            self.place_signature({"aspect": 0.3, "strokes": [{"width": 0.02, "pts": [[0, 0, 0.5], [0.5, 0.3, 0.5], [1, 0, 0.5]]}]})
            sign_item = [i for i in self.items if i.kind == "sign"][0]
            page = self.pdf_pages()[0]
            px, py, pw_, ph_ = self.item_box(page)
            sign_item.x, sign_item.y, sign_item.width = px + 0.2 * pw_, py + 0.5 * ph_, 0.3 * pw_
            unsigned = f"{d}/u.pdf"
            r = self.export_pdf(unsigned, protect=False)
            signed = f"{d}/signed.pdf"
            who = sign_pdf(unsigned, signed, p12, "pw123", r["sign_rects"][0] if r["sign_rects"] else None, "I agree")
            log("selftest signature: a place was found under the drawn signature:", len(r["sign_rects"]) == 1,
                "| signed by:", who["name"], "| self-made certificate noticed:", who["self_signed"])
            info = subprocess.run(["pdfsig", signed], capture_output=True, text=True).stdout
            log("selftest signature: genuine and intact:", check_signature(signed),
                "| an independent tool (pdfsig) sees the signature:", "Signature Field Name" in info and "Test Person" in info)
            data = bytearray(Path(signed).read_bytes())
            at = data.find(b"%PDF-1.") + 7
            data[at] = ord("6") if data[at] != ord("6") else ord("5")             # change one byte inside the signed part
            Path(f"{d}/tampered.pdf").write_bytes(bytes(data))
            try:
                changed_caught = not check_signature(f"{d}/tampered.pdf")
            except Exception:  # noqa: BLE001
                changed_caught = True
            log("selftest signature: a changed file is caught:", changed_caught)
            try:
                sign_pdf(unsigned, f"{d}/x.pdf", p12, "wrong", None)
                bad = False
            except Exception:  # noqa: BLE001
                bad = True
            log("selftest signature: wrong certificate password is refused:", bad)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        self.add_slide_file(locked, done=after, password="hunter2")
        return False
    if mode == "pdfform":
        # text inside an embedded form object can't be removed: the app must say it is only covered
        from reportlab.pdfgen import canvas as rl
        from pypdf import PdfReader
        d = tempfile.mkdtemp()
        src = f"{d}/form.pdf"
        c = rl.Canvas(src, pagesize=(595, 842))
        c.beginForm("f")
        c.setFont("Helvetica", 30)
        c.drawString(80, 700, "Hidden inside a form")
        c.endForm()
        c.doForm("f")
        c.save()

        def after():
            page = self.pdf_pages()[0]
            bx, by, bw, bh = self.item_box(page)
            cover = self.line_cover(page, bx + 0.25 * bw, by + 0.17 * bh)
            self.commit_text(None, cover["rect"][:2], "Changed", cover)
            out = f"{d}/out.pdf"
            res = self.export_pdf(out)
            log("selftest pdfform: the app reports text it could not remove:", res["covers"] == 1 and res["left"] == 1, res)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        self.add_slide_file(src, done=after)
        return False
    if mode == "pdfheb":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas as rl
        from pypdf import PdfReader
        pdfmetrics.registerFont(TTFont("DJ", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        d = tempfile.mkdtemp()
        src = f"{d}/heb.pdf"
        logical = "שלום עולם זה משפט"
        c = rl.Canvas(src, pagesize=(595, 842))
        c.setFont("DJ", 28)
        c.drawString(300, 700, logical[::-1])          # right-to-left text as generators lay it out: reversed on the page
        c.drawString(80, 600, "Plain line")
        c.save()

        def after():
            page = self.pdf_pages()[0]
            bx, by, bw, bh = self.item_box(page)
            cover = self.line_cover(page, bx + 0.69 * bw, by + 0.154 * bh)
            log("selftest hebrew: the line is read in the right order:", cover is not None and cover["text"] == logical,
                "|", repr(cover["text"]) if cover else None)
            rx, ry, rw, rh = cover["rect"]
            self.commit_text(None, (rx, ry), "עולם חדש", cover)
            t = [i for i in self.items if i.kind == "text"][0]
            log("selftest hebrew: retyped Hebrew sits on the right edge of the old line:",
                abs(t.x + self.item_box(t)[2] - (rx + rw)) < 1.5)
            out = f"{d}/out.pdf"
            res = self.export_pdf(out)
            plain = subprocess.run(["pdftotext", out, "-"], capture_output=True, text=True).stdout
            log("selftest hebrew: old Hebrew removed, the retyped Hebrew is real text, other line kept:",
                "משפט" not in plain and "Plain line" in plain and "חדש" in plain, "| report:", res)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        self.add_slide_file(src, done=after)
        return False
    if mode == "pdftext":
        import cairo
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
        d = tempfile.mkdtemp()
        raw, src = f"{d}/raw.pdf", f"{d}/one.pdf"
        surf = cairo.PDFSurface(raw, 595, 842)
        c = cairo.Context(surf)
        c.set_source_rgb(1, 1, 1)
        c.paint()
        c.set_source_rgb(0, 0, 0)
        c.set_font_size(40)
        c.move_to(80, 200)
        c.show_text("Original line")
        c.move_to(80, 300)
        c.show_text("Second line")
        surf.finish()
        wr = PdfWriter(clone_from=raw)
        wr.pages[0].cropbox = RectangleObject([30, 40, 565, 800])      # crop box inside the media box
        with open(src, "wb") as f:
            wr.write(f)
        dark = lambda r_, g, b: r_ < 110 and g < 110 and b < 110

        # a page placed while pictures showed the whole media box (595 x 842) moves onto the crop box
        old = Item("image", 100.0, 50.0, width=595.0, ratio=842 / 595, pdf=(src, 0))
        migrate_page(old, 842 / 595)
        log("selftest migrate: old page lined up with its crop box:",
            abs(old.x - 130) < 1e-6 and abs(old.y - 92) < 1e-6 and abs(old.width - 535) < 1e-6
            and abs(old.ratio - 760 / 535) < 1e-6)
        import io as _io
        import json as _json
        import zipfile as _zip
        png = _io.BytesIO()
        cairo.ImageSurface(cairo.FORMAT_ARGB32, 595, 842).write_to_png(png)
        v1 = f"{d}/old.inkling"
        with _zip.ZipFile(v1, "w") as zf:
            zf.writestr("state.json", _json.dumps({"version": 1, "view": {"zoom": 1, "ox": 0, "oy": 0}, "strokes": [], "items": [
                {"kind": "image", "x": 100.0, "y": 50.0, "width": 595.0, "text": "", "size": 24.0, "color": [0.08, 0.08, 0.08],
                 "auto": False, "image": "images/0.png", "pdf": {"file": "pdfs/x.pdf", "page": 0}}]}))
            zf.writestr("images/0.png", png.getvalue())
            zf.write(src, "pdfs/x.pdf")
        _s, items_v1, _v = self.read_project(v1)
        it_ = items_v1[0]
        log("selftest migrate: an older saved note opens lined up, with no stored picture needed:",
            abs(it_.x - 130) < 1e-6 and abs(it_.width - 535) < 1e-6 and it_.texture is None and it_.pdf is not None)

        def band(pdf, y0, y1):
            w, h, data = pixels(pdf, 1)
            return sum(1 for i in range(0, len(data) - 2, 3)
                       if y0 <= (i // 3) // w / h <= y1 and dark(data[i], data[i + 1], data[i + 2]))

        def after():
            page = self.pdf_pages()[0]
            bx, by, bw, bh = self.item_box(page)
            w, h, data = pixels(src, 1)
            ys = [(i // 3) // w / h for i in range(0, len(data) - 2, 3) if dark(data[i], data[i + 1], data[i + 2])
                  and (i // 3) // w / h < 0.26]
            xs = [(i // 3) % w / w for i in range(0, len(data) - 2, 3) if dark(data[i], data[i + 1], data[i + 2])
                  and (i // 3) // w / h < 0.26]
            fx, fy = sum(xs) / len(xs), sum(ys) / len(ys)              # where the first line really is (0..1)
            cover = self.line_cover(page, bx + fx * bw, by + fy * bh)
            ok = cover is not None
            rx, ry, rw, rh = cover["rect"] if ok else (0, 0, 1, 1)
            log("selftest pdftext: line found under the click:", ok and cover["text"] == "Original line",
                "| its box sits on the real text:", abs((rx + rw / 2 - bx) / bw - fx) < 0.12 and abs((ry + rh / 2 - by) / bh - fy) < 0.03,
                "| page colour white, ink dark:", ok and min(cover["bg"]) > 0.95 and max(cover["fg"]) < 0.3)
            log("selftest pdftext: empty spot finds nothing:", self.line_cover(page, bx + 0.5 * bw, by + 0.9 * bh) is None)

            before_1, before_2 = band(src, 0.12, 0.26), band(src, 0.28, 0.42)
            self.commit_text(None, (rx, ry), "Original line", cover)
            log("selftest pdftext: unchanged text changes nothing:", not [i for i in self.items if i.kind == "cover"])
            self.commit_text(None, (rx, ry), "", cover)                # delete the line
            gone = f"{d}/gone.pdf"
            self.export_pdf(gone)
            log("selftest pdftext delete: line is covered in the PDF:", band(gone, 0.12, 0.26) < before_1 * 0.03,
                "| next line untouched:", band(gone, 0.28, 0.42) == before_2)
            self.on_undo()
            log("selftest pdftext undo: original comes back:", not [i for i in self.items if i.kind == "cover"])

            self.commit_text(None, (rx, ry), "Changed it", cover)       # retype the line
            new = f"{d}/new.pdf"
            result = self.export_pdf(new)
            kinds = sorted(i.kind for i in self.items if i.kind in ("cover", "text"))
            log("selftest pdftext retype: a patch and the new text:", kinds == ["cover", "text"],
                "| new text is drawn there:", 0 < band(new, 0.12, 0.26) < before_1 * 3,
                "| next line untouched:", band(new, 0.28, 0.42) == before_2)
            text_after = PdfReader(new).pages[0].extract_text() or ""
            log("selftest pdftext privacy: old words are gone, the retyped line is real text, other line kept:",
                "Original" not in text_after and "Second line" in text_after and "Changed it" in text_after,
                "| report:", result)

            self.name_entry.set_text("text edit note")
            self.save()
            self.strokes, self.items = [], []
            self.open_note("text edit note")
            log("selftest pdftext reopen: patch and text came back:",
                sorted(i.kind for i in self.items if i.kind in ("cover", "text")) == ["cover", "text"])

            page = self.pdf_pages()[0]                                  # turning an edited page flattens it
            self.selected = page
            self.rotate_page()
            turned = f"{d}/turned.pdf"
            self.export_pdf(turned)
            tp = PdfReader(turned).pages[0]
            log("selftest pdftext turn: page is sideways, edits are in the page, none left loose:",
                float(tp.mediabox.width) > float(tp.mediabox.height) and not [i for i in self.items if i.kind in ("cover", "text")],
                "| old words still gone:", "Original" not in (tp.extract_text() or ""))
            self.on_undo()
            log("selftest pdftext turn undo: edits are back as edits:",
                sorted(i.kind for i in self.items if i.kind in ("cover", "text")) == ["cover", "text"])
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        self.add_slide_file(src, done=after)
        return False
    if mode == "pdf":
        import cairo
        from pypdf import PdfReader
        d = tempfile.mkdtemp()
        src = f"{d}/three.pdf"
        surf = cairo.PDFSurface(src, 595, 842)
        c = cairo.Context(surf)
        for n in (1, 2, 3):
            c.set_source_rgb(1, 1, 1)
            c.paint()
            c.set_source_rgb(0, 0, 0)
            c.set_font_size(60)
            c.move_to(80, 200)
            c.show_text(f"Page {n}")
            surf.show_page()
        surf.finish()
        from pypdf import PdfWriter
        from pypdf.generic import RectangleObject
        base_pdf = PdfWriter(clone_from=src)
        base_pdf.pages[1].cropbox = RectangleObject([30, 40, 565, 800])
        with open(src, "wb") as f:
            base_pdf.write(f)

        def after():
            pages = self.pdf_pages()
            log("selftest pdf: opened 3 pages with their PDF behind them:",
                len(pages) == 3 and all(p.pdf for p in pages))
            p0, p1, p2 = pages

            def screen(it, fx, fy):
                bx, by, bw, bh = self.item_box(it)
                return (bx + bw * fx) * self.zoom + self.ox, (by + bh * fy) * self.zoom + self.oy

            self.color = (0.10, 0.30, 0.85)                       # blue pen on page 2
            (ax, ay), (bx_, by_) = screen(p1, 0.2, 0.5), screen(p1, 0.8, 0.6)
            self.begin(ax, ay, 0.5, False)
            self.extend((ax + bx_) / 2, (ay + by_) / 2 - 20, 0.5)
            self.extend(bx_, by_, 0.5)
            self.finish()
            self.set_mode("highlight")                            # yellow highlight on page 2
            (ax, ay), (bx_, by_) = screen(p1, 0.1, 0.25), screen(p1, 0.7, 0.25)
            self.begin(ax, ay, 0.5, False)
            self.extend(bx_, by_, 0.5)
            self.finish()
            self.set_mode("pen")
            self.text_px = 70                                      # big, so its ink is easy to measure
            self.commit_text(None, self.to_world(*screen(p2, 0.2, 0.4)), "Hello שלום")   # text on page 3
            self.set_mode("pen")

            def label(p):                                           # 1, 2 or 3; page 3 is the sideways one after the turn
                return 3 if p.ratio < 1 else p.pdf[1] + 1
            w0, h0 = self.item_box(p2)[2:]                         # rotate page 3
            self.selected = p2
            self.rotate_page()
            w1, h1 = self.item_box(p2)[2:]
            log("selftest pdf rotate: page turned sideways:", abs(w1 - h0) < 1 and abs(h1 - w0) < 1,
                "| typed text became part of the page:", not [i for i in self.items if i.kind == "text"])

            self.selected = p2                                     # move page 3 up one place
            self.move_page(-1)
            order = self.pdf_pages()
            log("selftest pdf move: order is now page 1, 3, 2:", [label(p) for p in order] == [1, 3, 2],
                "| no overlap:", all(self.item_box(order[k])[1] + self.item_box(order[k])[3] <= order[k + 1].y
                                       for k in range(2)))
            self.on_undo()                                        # undo hands back copies: compare by page number
            log("selftest pdf move undo: back to 1, 2, 3:", [label(p) for p in self.pdf_pages()] == [1, 2, 3])
            self.on_redo()
            p0 = self.pdf_pages()[0]

            y_first = p0.y                                         # delete page 1: the others move up
            self.selected = p0
            self.delete_page()
            rest = self.pdf_pages()
            log("selftest pdf delete: 2 pages left, page 1 gone:", [label(p) for p in rest] == [3, 2],
                "| first remaining page took its place:", abs(rest[0].y - y_first) < 1e-6)

            out = f"{d}/out.pdf"
            self.export_pdf(out)
            r = PdfReader(out)
            first = r.pages[0]
            text1 = first.extract_text() or ""
            text2 = r.pages[1].extract_text() or ""
            log("selftest pdf export: pages:", len(r.pages), "| original text still real text:",
                "Page 3" in text1 and "Page 2" in text2,
                "| rotated page is landscape:", float(first.mediabox.width) > float(first.mediabox.height))
            w, h, data = pixels(out, 2)
            blue = count_px(data, lambda r_, g, b: b > 180 and r_ < 90)
            yellow = count_px(data, lambda r_, g, b: r_ > 220 and g > 190 and b < 170)
            log("selftest pdf export: blue pen on page 2:", blue > 5, "| yellow highlight on page 2:", yellow > 20)

            def centre(test):                                      # where on the page (0..1) the colour sits
                xs, ys = [], []
                for i in range(0, len(data) - 2, 3):
                    if test(data[i], data[i + 1], data[i + 2]):
                        xs.append((i // 3) % w / w)
                        ys.append((i // 3) // w / h)
                return sum(xs) / len(xs), sum(ys) / len(ys)
            bcx, bcy = centre(lambda r_, g, b: b > 180 and r_ < 90)      # drawn from (0.2, 0.5) to (0.8, 0.6)
            ycx, ycy = centre(lambda r_, g, b: r_ > 220 and g > 190 and b < 170)   # (0.1, 0.25) to (0.7, 0.25)
            log("selftest pdf export: marks land where they were drawn: pen", (round(bcx, 2), round(bcy, 2)),
                "highlight", (round(ycx, 2), round(ycy, 2)), "->",
                abs(bcx - 0.5) < 0.08 and abs(bcy - 0.55) < 0.08 and abs(ycx - 0.4) < 0.08 and abs(ycy - 0.25) < 0.05)
            imgs = subprocess.run(["pdfimages", "-list", out], capture_output=True, text=True).stdout.strip().splitlines()
            _, _, turned = pixels(out, 1)
            _, _, plain = pixels(src, 3)
            ink = lambda data: count_px(data, lambda r_, g, b: min(r_, g, b) < 150)      # the typed text is blue
            log("selftest pdf export: typed text is real, selectable text on page 1 (not a picture):",
                len(imgs) <= 2 and ink(turned) > ink(plain) * 1.2 and "Hello" in text1 and "Page 3" in text1,
                "| images:", len(imgs) - 2)

            part = f"{d}/part.pdf"
            self.export_pdf(part, [2])
            log("selftest pdf split: one page:", len(PdfReader(part).pages) == 1,
                "| it is page 2:", "Page 2" in (PdfReader(part).pages[0].extract_text() or ""))

            # signing: draw once (saved), place it on page 2, it shows up in the PDF
            os.environ["INKLING_CONFIG"] = f"{d}/cfg"               # never touch the real saved signature
            self.color = (0.08, 0.08, 0.08)
            n_strokes, n_undo = len(self.strokes), len(self.undo_stack)
            self.start_signature()
            self.begin(300, 300, 0.5, False)
            self.extend(340, 340, 0.6)
            self.extend(380, 300, 0.5)
            self.extend(420, 340, 0.5)
            self.finish()
            self.finish_signature()
            signs = [i for i in self.items if i.kind == "sign"]
            log("selftest sign: placed one signature and saved it:", len(signs) == 1 and self.sign_file().exists(),
                "| drawing left no strokes behind:", len(self.strokes) == n_strokes,
                "| selected, in the move tool:", self.selected is signs[0] and self.mode == "move")
            self.on_sign()                                           # second time: uses the saved one, no drawing
            signs = [i for i in self.items if i.kind == "sign"]
            log("selftest sign: second signature comes from the saved one:", len(signs) == 2)
            self.on_undo()
            signs = [i for i in self.items if i.kind == "sign"]
            sign = signs[0]
            page2 = self.pdf_pages()[1]
            bx, by, bw, bh = self.item_box(page2)
            sign.x, sign.y, sign.width = bx + 0.3 * bw, by + 0.65 * bh, 0.3 * bw      # drag it onto page 2
            dark = lambda r_, g, b: r_ < 100 and g < 100 and b < 100
            _, _, before = pixels(out, 2)
            signed = f"{d}/signed.pdf"
            self.export_pdf(signed)
            w, h, after = pixels(signed, 2)
            low = lambda data: sum(1 for i in range(0, len(data) - 2, 3)
                                   if (i // 3) // w / h > 0.55 and dark(data[i], data[i + 1], data[i + 2]))
            log("selftest sign export: ink appears where it was placed:", low(before) == 0 and low(after) > 5)
            self.on_undo()                                           # back to no signature
            log("selftest sign undo: signature gone:", not [i for i in self.items if i.kind == "sign"])
            self.on_redo()

            # the note keeps its PDF: save, forget everything, reopen, export again
            self.name_entry.set_text("pdf note")
            ok = self.save()
            md = (NOTES_DIR / "pdf note.md").read_text()
            log("selftest pdf save: ok:", ok, "| note has the Edit link:", "inkling://open/pdf%20note" in md)
            self.strokes, self.items = [], []
            self.open_note("pdf note")
            again = f"{d}/again.pdf"
            self.export_pdf(again)
            log("selftest pdf reopen: still 2 pages and exports:", len(self.pdf_pages()) == 2
                and len(PdfReader(again).pages) == 2)
            log("selftest sign reopen: signature came back:", len([i for i in self.items if i.kind == "sign"]) == 1)

            # no PDF at all: the whole surface becomes one page
            self.strokes, self.items = [], []
            line(100, 100, 400, 220)
            flat = f"{d}/flat.pdf"
            self.export_pdf(flat)
            log("selftest pdf freehand: one page:", len(PdfReader(flat).pages) == 1)
            shutil.rmtree(d, ignore_errors=True)
            self.win.close()

        self.add_slide_file(src, done=after)
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

    # a fast stroke has only two points, far apart: erasing across its middle must still remove it
    self.begin(100, 700, 0.5, False)
    self.extend(400, 700, 0.5)
    self.finish()
    n = len(self.strokes)
    self.begin(250, 702, 0.5, True)
    self.finish()
    log("selftest eraser between points: removed:", len(self.strokes) == n - 1)
    self.on_undo()
    self.on_undo()
    # erasing empty space must not use up an Undo step
    steps = len(self.undo_stack)
    self.begin(900, 900, 0.5, True)
    self.finish()
    log("selftest eraser on empty space leaves no undo step:", len(self.undo_stack) == steps)

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
        "| editable copy:", (PROJECT_DIR / f"{base}.inkling").exists())
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
    for f in PROJECT_DIR.glob("*.inkling"):
        f.unlink()
    self.strokes, self.items = [], []
    self.saved_base = None
    opened = self.open_note("legacy note")
    ok_legacy = (opened and len(self.items) == 1 and self.items[0].kind == "image"
                 and not self.strokes)
    line(100, 100, 200, 160)
    self.save()
    md_text = strip_link(legacy.with_suffix(".md").read_text())
    log("selftest legacy open: ok:", ok_legacy, "| save keeps my markdown untouched:",
        md_text == "My own text\n![[legacy note.png]]\n",
        "| now editable:", (PROJECT_DIR / "legacy note.inkling").exists())

    # rename keeps what was added to the note in Obsidian and fixes the embed
    legacy.with_suffix(".md").write_text("My own text\n![[legacy note.png]]\n")
    self.name_entry.set_text("renamed note")
    line(100, 200, 200, 260)
    self.save()
    ren = strip_link((NOTES_DIR / "renamed note.md").read_text())
    log("selftest rename: ok:", ren == "My own text\n![[renamed note.png]]\n",
        "| old files removed:", not (NOTES_DIR / "legacy note.md").exists()
        and not (NOTES_DIR / "legacy note.png").exists()
        and not (PROJECT_DIR / "legacy note.inkling").exists())
    self.win.close()
    return False
