# Figure comparison

[← back to README](README.md)

Beyond the text diff, `latexdiff-zip` also compares **figures**. For every figure that
changed between the two versions it builds a side-by-side collage with red **OLD** / green
**NEW** banners and — by default — appends those collages as extra pages at the end of the
diff PDF.

Figures are paired by their `\label` (parsed from the flattened source), so a figure still
matches even if its image file was **renamed** (e.g. `plot_v1.pdf` → `plot_v2.pdf`).
Unlabelled figures fall back to matching by document order. Source images in any common
format (`pdf`, `eps`, `png`, `jpg`, …) are rasterised to PNG before being composed.

- Pass `-F` to skip embedding the collages in the PDF.
- Pass `-c DIR` to also (or instead) write the collage PNGs into a folder.

## Requirements

The figure diff is optional and degrades gracefully — without these you still get the text
diff:

- **ImageMagick** (`magick`, or `convert`/`identify`) — builds the collages.
- **pdfunite** (poppler) or **gs** (Ghostscript) — appends them to the PDF.
- **python3** — does the `\label`-based pairing; without it, figures are matched by filename.
