# Inkling

A boundless canvas for a pen (or mouse): draw, write, mark up PDFs, sign them, and convert files.

- **Canvas and tools:** pen, highlighter, eraser, text boxes you type in place, a Select tool (click or drag a
  box around things), pictures, zoom and pan without limits.
- **PDF work:** open a PDF and write on it, highlight, retype its text, turn/delete/move pages, split or join,
  sign with a drawn signature or a digital certificate, open password-protected files.
- **Word, slides and spreadsheets:** opened as pages you can write on. If you have WordCraft, DeckCraft and
  GridCraft, Inkling finds them and uses each for its own file types.
- **Tabs:** every document opens in its own tab in the left panel.
- **Export:** PDF, Word, PowerPoint, OpenDocument, RTF, plain text, pictures, or an `.inkling` copy of the note.

## Where things are saved

- **Automatically** (Save, Ctrl+S, closing a tab or the window): in Inkling's own folder, `Documents/Inkling`
  by default. Change it in the left panel under *Saved in*. It holds a picture and a small markdown note of each
  note, and a hidden `.inkling` folder with the editable copies.
- **When you choose:** *Export* always asks where to save, and *Export > Inkling note* (Ctrl+Shift+S) saves a
  copy of the note anywhere you like.

## Install (Linux with a desktop)

```
git clone <this repository> inkling && cd inkling
./install.sh            # launcher entry + pen watcher;   ./install.sh --no-pen  for the launcher only
```

It needs GTK 4 for Python, `poppler-utils`, `pypdf` and `pycairo`; the script tells you the package names for
your system if anything is missing. Optional: `pyhanko` (digital signatures), `python3-gi-cairo` (text stays real,
selectable text in PDFs), LibreOffice or the Craft apps (Word, PowerPoint and Excel files).

The pen watcher reads the pen's input device, so it needs a Linux computer with a pen display. Everything else
works with a mouse. Inkling is a GTK 4 program for Linux; it does not run on Windows or macOS.

## Tests

`INKLING_SELFTEST=<mode> INKLING_DIR=$(mktemp -d) python3 inkling.py canvas` runs one group of checks (see
`selftest.py` for the modes). `INKLING_DIR` must point at a scratch folder: the tests save and delete notes there.
