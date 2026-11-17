"""The converter window: drop files in, pick what you want, convert. The engine is converter.py."""
import threading
import time
from collections import Counter
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

KIND_ICONS = {"document": "x-office-document", "slides": "x-office-presentation", "spreadsheet": "x-office-spreadsheet",
              "image": "image-x-generic", "archive": "package-x-generic", "data": "text-x-generic",
              "audio": "audio-x-generic", "video": "video-x-generic"}


def human_size(path):
    try:
        n = Path(path).stat().st_size if Path(path).is_file() else sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class ConverterWindow(Gtk.Window):
    def __init__(self, app, conv, files=()):
        super().__init__(title="Convert", transient_for=app.win, default_width=640, default_height=720)
        self.app, self.conv = app, conv
        self.entries = []                       # one per file: {"path", "fmt", "row", "status", "show"}
        self.target_ids = []
        self.out_dir = None                     # None: next to each original
        self.cancel = threading.Event()
        self.running = False
        self.last_targets = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=14, margin_bottom=14,
                       margin_start=16, margin_end=16)
        self.set_child(root)
        title = Gtk.Label(label="Convert anything to anything", xalign=0)
        title.add_css_class("title-2")
        root.append(title)
        sub = Gtk.Label(label="Drop files or folders here. Inkling finds the best route and uses the apps on this computer.",
                        xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        root.append(sub)

        bar = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add files…")
        add.connect("clicked", self.on_add_files)
        add_dir = Gtk.Button(label="Add a folder…")
        add_dir.connect("clicked", self.on_add_folder)
        self.clear_btn = Gtk.Button(label="Clear")
        self.clear_btn.connect("clicked", self.on_clear)
        for b in (add, add_dir, self.clear_btn):
            bar.append(b)
        root.append(bar)

        self.drop_hint = Gtk.Label(label="Drop files here", margin_top=22, margin_bottom=22)
        self.drop_hint.add_css_class("dim-label")
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_placeholder(self.drop_hint)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=190)
        scroller.set_child(self.listbox)
        root.append(scroller)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)

        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Convert to"))
        self.dropdown = Gtk.DropDown(model=Gtk.StringList.new([]), hexpand=True)
        self.dropdown.connect("notify::selected", lambda *_: self.show_route())
        row.append(self.dropdown)
        root.append(row)
        self.route = Gtk.Label(label="", xalign=0, wrap=True)
        self.route.add_css_class("dim-label")
        root.append(self.route)

        out = Gtk.Box(spacing=8)
        self.beside = Gtk.CheckButton(label="Save next to each original", active=True)
        self.beside.connect("toggled", lambda *_: self.update_folder_button())
        self.folder_btn = Gtk.Button(label="Choose a folder…")
        self.folder_btn.connect("clicked", self.on_choose_folder)
        out.append(self.beside)
        out.append(self.folder_btn)
        root.append(out)
        self.sharp = Gtk.CheckButton(label="Sharper pictures from pages (300 dpi: slower, bigger)")
        root.append(self.sharp)

        self.progress = Gtk.ProgressBar(visible=False)
        root.append(self.progress)
        self.summary = Gtk.Label(label="", xalign=0, wrap=True)
        root.append(self.summary)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.connect("clicked", lambda *_: self.close())
        self.go = Gtk.Button(label="Convert")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", self.on_convert)
        buttons.append(self.close_btn)
        buttons.append(self.go)
        root.append(buttons)
        self.connect("close-request", self.on_close)
        self.update_folder_button()
        self.add_paths(files)
        self.refresh()

    # ---- adding things --------------------------------------------------------------------------------------------
    def on_add_files(self, *_):
        dlg = Gtk.FileDialog(title="Choose files to convert")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.add_paths([f.get_path() for f in files if f.get_path()])
        dlg.open_multiple(self, None, done)

    def on_add_folder(self, *_):
        dlg = Gtk.FileDialog(title="Choose a folder")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.add_paths([f.get_path()])
        dlg.select_folder(self, None, done)

    def on_drop(self, _target, value, _x, _y):
        if isinstance(value, Gdk.FileList):
            self.add_paths([f.get_path() for f in value.get_files() if f.get_path()])
            return True
        return False

    def add_paths(self, paths):
        known = {e["path"] for e in self.entries}
        for p in paths:
            p = str(p)
            if p in known or not Path(p).exists():
                continue
            fmt = self.conv.detect(p)
            entry = {"path": p, "fmt": fmt, "status": None}
            entry["row"] = self.make_row(entry)
            self.listbox.append(entry["row"])
            self.entries.append(entry)
        self.refresh()

    def make_row(self, entry):
        fmt = entry["fmt"]
        row = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=10, margin_end=8)
        kind = self.conv.kind_of(fmt) if fmt else None
        icon = Gtk.Image.new_from_icon_name(KIND_ICONS.get(kind, "folder" if fmt == "folder" else "dialog-question"))
        icon.set_pixel_size(24)
        row.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=Path(entry["path"]).name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        what = self.conv_label(fmt)
        info = Gtk.Label(label=f"{what}  ·  {human_size(entry['path'])}", xalign=0)
        info.add_css_class("dim-label")
        info.add_css_class("caption")
        text.append(name)
        text.append(info)
        row.append(text)
        entry["status"] = Gtk.Label(label="", xalign=1, ellipsize=Pango.EllipsizeMode.END, max_width_chars=34)
        row.append(entry["status"])
        entry["show"] = Gtk.Button(label="Show", visible=False)
        entry["show"].add_css_class("flat")
        entry["show"].connect("clicked", lambda *_: self.show_in_folder(entry))
        row.append(entry["show"])
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self.remove(entry))
        row.append(remove)
        return row

    def conv_label(self, fmt):
        return self.conv.label(fmt) if fmt else "unknown kind of file"

    def remove(self, entry):
        if self.running:
            return
        self.listbox.remove(entry["row"].get_parent())
        self.entries.remove(entry)
        self.refresh()

    def on_clear(self, *_):
        if self.running:
            return
        for e in list(self.entries):
            self.listbox.remove(e["row"].get_parent())
        self.entries.clear()
        self.refresh()

    # ---- what can they become -----------------------------------------------------------------------------------------
    def refresh(self):
        """Work out the targets all these files can reach, and update the list and the buttons."""
        per = [(e, self.conv.targets(e["fmt"]) if e["fmt"] else {}) for e in self.entries]
        counts, cheapest = Counter(), {}
        for _e, t in per:
            for target, (cost, _path) in t.items():
                counts[target] += 1
                cheapest[target] = min(cheapest.get(target, 99), cost)
        total = len(self.entries)
        order = sorted(counts, key=lambda t: (-counts[t], cheapest[t], self.conv.label(t)))
        old = self.target_ids[self.dropdown.get_selected()] if self.target_ids and self.dropdown.get_selected() < len(self.target_ids) else None
        self.target_ids = order
        labels = [self.conv.label(t) + (f"  ({counts[t]} of {total} files)" if counts[t] < total else "") for t in order]
        self.dropdown.set_model(Gtk.StringList.new(labels))
        if old in order:
            self.dropdown.set_selected(order.index(old))
        for e, t in per:
            if not self.running and e["status"] is not None and not e.get("done"):
                e["status"].set_text("" if t else self.conv.why_not(e["fmt"])[:80])
        self.go.set_sensitive(bool(order) and not self.running)
        self.clear_btn.set_sensitive(bool(self.entries) and not self.running)
        self.show_route()

    def selected_target(self):
        i = self.dropdown.get_selected()
        return self.target_ids[i] if 0 <= i < len(self.target_ids) else None

    def show_route(self):
        target = self.selected_target()
        if target is None:
            self.route.set_text("" if self.entries else "Add some files to begin.")
            return
        for e in self.entries:
            t = self.conv.targets(e["fmt"]) if e["fmt"] else {}
            if target in t:
                where, tools, notes = self.conv.describe(e["fmt"], t[target][1])
                self.route.set_text(f"{where}   ·   {tools}" + (f"   ·   {notes}" if notes else ""))
                return

    def update_folder_button(self):
        self.folder_btn.set_sensitive(not self.beside.get_active())
        self.folder_btn.set_label("Choose a folder…" if not self.out_dir else f"Folder: {Path(self.out_dir).name}")

    def on_choose_folder(self, *_):
        dlg = Gtk.FileDialog(title="Where should the results go?")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.out_dir = f.get_path()
            self.update_folder_button()
        dlg.select_folder(self, None, done)

    # ---- converting ------------------------------------------------------------------------------------------------------
    def on_convert(self, *_):
        if self.running:
            self.cancel.set()
            self.go.set_label("Stopping…")
            return
        target = self.selected_target()
        if target is None:
            return
        out = None if self.beside.get_active() else self.out_dir
        if out is None and not self.beside.get_active():
            self.summary.set_text("Choose a folder first, or save next to the originals.")
            return
        jobs, owners = [], []
        for e in self.entries:
            e["done"] = False
            if e["fmt"] and target in self.conv.targets(e["fmt"]):
                jobs.append((e["path"], target))
                owners.append(e)
                e["status"].set_text("waiting…")
            else:
                e["status"].set_text("skipped: can't become this")
            e["show"].set_visible(False)
        if not jobs:
            return
        self.cancel.clear()
        self.running = True
        self.finished = 0
        self.t0 = time.monotonic()
        self.failed = 0
        self.go.set_label("Stop")
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.summary.set_text("")
        self.close_btn.set_sensitive(False)
        hq = self.sharp.get_active()

        def on_result(i, outcome):
            GLib.idle_add(self.got_result, owners, jobs, i, outcome)

        def work():
            self.conv.convert_many(jobs, out, hq=hq, cancel=self.cancel, on_result=on_result)
            GLib.idle_add(self.all_done, len(jobs))
        threading.Thread(target=work, daemon=True).start()

    def got_result(self, owners, jobs, i, outcome):
        e = owners[i]
        e["done"] = True
        self.finished += 1
        if outcome["ok"]:
            first = outcome["outputs"][0]
            e["status"].set_text(f"✓ {first.name}" + (f" +{len(outcome['outputs']) - 1}" if len(outcome["outputs"]) > 1 else ""))
            e["status"].set_tooltip_text(f"{outcome['route']}\n{outcome['seconds']:.1f} s")
            e["show"].set_visible(True)
            e["result"] = outcome["outputs"]
        else:
            self.failed += 1
            e["status"].set_text(f"✗ {outcome['error']}"[:200])
            e["status"].set_tooltip_text(outcome["error"])
        self.progress.set_fraction(self.finished / len(jobs))
        return False

    def all_done(self, n):
        self.running = False
        took = time.monotonic() - self.t0
        stopped = self.cancel.is_set()
        self.summary.set_text(("Stopped. " if stopped else "") + f"{n - self.failed} converted"
                              + (f", {self.failed} could not be" if self.failed else "") + f"  ·  {took:.1f} s")
        self.progress.set_visible(False)
        self.go.set_label("Convert")
        self.close_btn.set_sensitive(True)
        self.refresh()
        return False

    def show_in_folder(self, entry):
        out = entry.get("result")
        if out:
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(Path(out[0]).parent)).get_uri(), None)
            except GLib.Error:
                pass

    def on_close(self, *_):
        if self.running:
            self.cancel.set()
        self.app.conv_win = None
        return False
