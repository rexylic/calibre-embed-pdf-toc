# Embed ToC — a Calibre plugin

Embed a navigable table of contents into PDF books in your Calibre library
from a plain-text TOC.

## What it does

- Operates on a single selected PDF book in your library.
- Lets you write the TOC in a text editor or a table view (both are kept in
  sync; the same data is shown two ways).
- Supports page labels: roman numerals (`v`, `ix`), arabic, or anything else
  the PDF's `/PageLabels` defines.
- Supports a global integer offset applied to all numeric page entries —
  useful when the printed page numbers in the source TOC are off from the
  PDF's labels by a constant.
- Backs up the original PDF on the first run and offers an Undo button
  after each subsequent embedding.
- Saves the TOC and the original-PDF backup in the book's `data/` folder
  (the same folder exposed by Calibre's *Open book data folder* action),
  so you can edit and re-run later without cluttering the main book folder.
- Validates every entry's page label against the PDF before writing; on
  miss, points you at the offending entry and returns you to the editor.
- After a successful write, prompts you to open the PDF in your OS's default
  viewer.

DjVu is not supported.

## TOC file format

```
# offset: 3
Preface v
Chapter 1 Introduction 1
    1.1 Background 3
    1.2 Outline 8
Chapter 2 Methods 15
```

- Indentation defines nesting (spaces or tabs; the smallest non-zero indent
  becomes the unit).
- Last whitespace-delimited token is the page label.
- `# offset: N` (optional) applies a global integer offset at write time,
  only when every entry's page is a positive integer.

## Files in the book's data folder

- `toc` — the plain-text TOC. Persistent; auto-loaded when you re-open the
  plugin on the same book.
- `<book>.original_pdf` — a snapshot of the original PDF, captured on the
  first successful run and never overwritten. Undo always restores from
  this file.

## Install

From the plugin folder:

```
calibre-customize -b .
```

Then in Calibre, open Preferences → Toolbars & menus and add the
**Embed ToC** action to whichever toolbar or menu you want. (The plugin
does not place itself anywhere by default.)

## Development loop

```
calibre-debug -s && calibre-customize -b /path/to/this/folder && calibre
```

Shuts down a running Calibre, re-installs the plugin, and relaunches.

Tests for the parser and PDF writer (no Calibre dependency):

```
python test_plugin.py
```
