# Figure comparison

[← back to README](README.md)

Beyond the text diff, `latexdiff-zip` also compares **figures**. It finds every figure that
changed between the two versions. By default, it adds a **Figure changes** appendix at the
end of the diff PDF that shows the OLD and NEW versions of each one.

## How figures are paired

The engine pairs figures by their `\label`, which it reads from the flattened source. So a
figure still matches if its image file was **renamed** (e.g. `plot_v1.pdf` → `plot_v2.pdf`).
Next, it pairs the remaining figures that use the same image file in both versions. Then it
pairs figures that have no `\label` on either side, in document order. If only one version has
a figure's label, the engine reports that figure as added or removed. It never pairs it with an
unrelated figure. The engine ignores commented-out figures (`% \includegraphics{…}`).

The engine finds figure files the way LaTeX does. It searches the document directory first,
then each `\graphicspath{{figs/}}` entry, so a bare name and an explicit `figs/plot` both work.
It tries a name without an extension in pdflatex's order (`.pdf` before `.png`). So the file it
compares is the file the paper shows.

## What is reported

The comparison reports three kinds of change:

- **Δ Changed**: a figure present in both versions whose image differs. The engine does not
  report a regenerated file that is *byte-different but pixel-identical*. (Matplotlib and
  Inkscape write a fresh timestamp into every export.) An image comparison finds these files.
- **+ Added**: a figure only in the new version.
- **− Removed**: a figure only in the old version. The diff also draws it in the body, where
  it used to be (see below).

## The appendix

The appendix is a LaTeX document of its own, added after the last page of the diff.

- **Overview.** The first page lists every change: its status, the figure, its page in the
  diff, and its page in the appendix. Click a row to go to that comparison. The PDF also gets
  a **Figure changes** bookmark with one entry per comparison.
- **Heading.** Each comparison starts with its status, the figure number, and its page in the
  diff. The caption follows as plain text, with any maths kept as TeX source. If the number is not
  reliable, the heading uses the `\label`, or else the file name.
- **Panels.** OLD is always on the left, or on top. Wide figures stack, and tall or square
  figures sit side by side, whichever shows them larger. The engine scales both images to
  fit the same box.
- **Added or removed figures** keep both slots. The empty slot is a dashed box that says
  "Not in OLD version" or "Not in NEW version".
- **Packing.** Comparisons follow the order of the paper. Small ones share a page, and a
  comparison never splits across two pages.
- **Page size.** The appendix uses the page size of the diff PDF. A page smaller than A5 or
  larger than A3 (slides, a poster) gets A4 in the same orientation.

The design is accessible, and color is never the only signal:

- The words OLD and NEW label every panel. Each status is a word and a symbol (Δ, +, −).
- OLD is dark red and NEW is dark blue, the same colors as the text diff. Both have a contrast
  of 7.5:1 on white.
- All text is real text, so you can search it, select it, and copy it. A grayscale print
  keeps all the information.
- Vector figures (`.pdf`, `.eps`) stay vector. They do not become blurry images.

## Figures in the body of the diff

The diff also draws figures that the new version **dropped or renamed**, in place in the
text. It uses latexdiff's `--graphics-markup=both`. This option draws a deleted figure at half size,
crossed out in red, instead of hiding it. The engine copies those old image files next to the
new project's files so that LaTeX can draw them. A file that the new project has under the same
name always wins.

This markup applies to every changed figure, not only to deleted ones. Where an image was
replaced, the diff body shows the old version crossed out next to the new one in a blue frame.
The appendix does not change because of this.

## Options

- Pass `-F` to leave the appendix out of the PDF.
- Pass `-c DIR` to also save each comparison as a PNG in `DIR`, named `<label>_diff.png`. The
  PNGs have the same design as the appendix.

## Requirements

The figure comparison is optional. Without these tools you still get the text diff:

- **ImageMagick** (`magick`, or `convert`/`identify`): converts each figure to PNG and does the
  pixel comparison that skips figures with no visible change.
- **Ghostscript** (`gs`): adds the appendix to the PDF and keeps the bookmarks and links of
  both. If `gs` is not installed, the engine uses `pdfunite` (poppler). Then the PDF loses its
  bookmarks, its internal links, and its title and author.
- The appendix uses the LaTeX packages `tgheros`, `tikz`, `hyperref` and `bookmark`. Without
  them, it falls back to a plain layout that needs only base LaTeX.
