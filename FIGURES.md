# Figure comparison

[← back to README](README.md)

Beyond the text diff, `latexdiff-zip` also compares **figures**. For every figure that
changed between the two versions it builds a side-by-side collage with red **OLD** / green
**NEW** banners and — by default — appends those collages as extra pages at the end of the
diff PDF.

Figures are paired by their `\label` (parsed from the flattened source), so a figure still
matches even if its image file was **renamed** (e.g. `plot_v1.pdf` → `plot_v2.pdf`).
Unlabelled figures fall back to matching by document order. Source images in any common
format (`pdf`, `eps`, `png`, `jpg`, …) are rasterised to PNG before being composed. Figures
referenced with a bare name under a `\graphicspath{{figs/}}` directory are resolved the way
LaTeX would (the document directory is searched first, then each `\graphicspath` entry, so an
explicit `figs/plot` keeps working too).

The comparison reports three kinds of change:

- **Changed** — a figure present in both versions whose image differs. Shown as an OLD vs NEW
  collage. A regenerated file that is *byte-different but pixel-identical* (matplotlib and
  Inkscape stamp a fresh timestamp into every export) is detected by an image comparison and
  **not** reported — this keeps the appendix free of false positives.
- **Added** — a figure only in the new version. Shown as a single panel with a green
  **NEW ONLY** banner.
- **Removed** — a figure only in the old version. Shown as a single panel with a red
  **OLD ONLY** banner, and also drawn in the body of the diff where it used to be (see
  below).

Figures the new version **dropped or renamed** are also rendered in place in the text: the
diff is run with latexdiff's `--graphics-markup=both`, which draws a deleted figure at half
size crossed out in red rather than commenting it away, and the engine copies those old image
files in beside the new project's assets so they can be drawn. A file the new project already
provides under that name is never overwritten.

That markup level applies to every changed figure, not only deleted ones: where an image was
replaced, the diff body now shows the old version crossed out next to the new one framed in
blue. The OLD/NEW collage pages are unaffected.

- Pass `-F` to skip embedding the collages in the PDF.
- Pass `-c DIR` to also (or instead) write the collage PNGs into a folder.

## Requirements

The figure diff is optional and degrades gracefully — without these you still get the text
diff:

- **ImageMagick** (`magick`, or `convert`/`identify`) — builds the collages and does the
  pixel-level comparison that skips visually-unchanged figures.
- **pdfunite** (poppler) or **gs** (Ghostscript) — appends them to the PDF.
