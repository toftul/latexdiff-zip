# Changelog

All notable changes to latexdiff-zip are in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version applies to the
CLI options and the outputs: the PDF, the log lines, and the `-c` PNG files.

`latexdiff-zip.py -V` prints the version. The web page shows it in the footer, and the
diff PDF names it in its Creator field.

## [Unreleased]

### Fixed

- Web UI: a long main `.tex` file name made its list wider than the page. The list now keeps
  its half of the row and ends a long name with "…". Hover over it to see the full name.
- Web UI: after a release, browsers could show the old example picture for up to four hours.
  Its URL now includes the version, so a new release loads the new picture.

## [2.3.1] - 2026-10-14

### Fixed

- Web UI: the page in the container showed no version in the footer. The web app could not
  import the installed engine (a file without the `.py` suffix), so it also used its fallback
  copy of the arXiv ID parser. It now imports the engine.

## [2.3.0] - 2026-10-14

### Added

- A version number. `-V` / `--version` prints it, each run logs it on the first line, the
  web page footer shows it, and the diff PDF names it in its Creator field.
- This changelog.
- The figure appendix starts with an overview table. Each row gives the status, the figure,
  its page in the diff, and its page in the appendix, and links to the comparison.
- The figure appendix adds a "Figure changes" bookmark with one entry per comparison.
- Each comparison names its figure: "Figure N", the page in the diff, and the caption as
  plain text. A panel of a multi-image figure says "panel i of n".
- Each panel names its source: the archive file name or the arXiv id.

### Changed

- LaTeX typesets the figure appendix. Before, ImageMagick drew it as image collages. The
  labels are now real text that you can search and copy, and vector figures stay vector.
- OLD is dark red and NEW is dark blue, the colors of the text diff (was red and green). The
  words OLD and NEW label every panel, and each status is a word and a symbol: Δ Changed,
  \+ Added, − Removed.
- Comparisons follow the order of the paper, and several share a page when they fit. OLD is
  on the left, or on top for wide figures. The engine scales both images to the same box.
- An added or removed figure keeps both slots. The empty slot is a dashed placeholder.
- The appendix uses the page size of the diff PDF. A size outside A5 to A3 gets A4.
- `-c DIR` PNGs have the same design as the appendix. A panel of a multi-image figure is saved
  as `<label>-p<i>_diff.png` (before, each panel overwrote the same file).
- Ghostscript adds the appendix to the PDF. `pdfunite` is now only the fallback.

### Fixed

- Adding the figure appendix removed the paper's bookmarks, title and author, and sent its
  internal links to page 1. Now all of them stay.
- A figure that the project ships as both `.pdf` and `.png` is compared as the `.pdf`, the file
  that pdflatex draws. Before, an edit to the PDF showed no figure change.
- REVTeX with a `.bib` printed each reference two or more times, and citations pointed into
  the extra copy. Each reference is now printed once.
- Commented-out figures were compared. They are now ignored.
- A new labeled figure was paired with an unrelated deleted figure. Now a labeled figure
  pairs only by label or by file, else it is reported as added or removed.

### Removed

- The collage pages with red OLD and green NEW banners, and the NEW ONLY / OLD ONLY panels.

## [2.2.0] - 2026-09-30

### Added

- `-t BLUE`: additions in blue, deletions not shown at all. The web page offers it too.

## [2.1.0] - 2026-09-04

### Added

- Web UI: picking or dropping an archive fills a list of its main `.tex` candidates. A click
  resolves an archive with several `\documentclass` files.
- A figure that the new version deleted or renamed is drawn in the diff body, crossed out.

### Changed

- Web UI: each archive uploads once, in its own request. Two large projects no longer pass the
  proxy's size limit together.
- Web UI: the markup style list describes what each style looks like.
- Web UI: the page title links to the home page.

### Fixed

- A figure with several panels under one `\label` compared unrelated images. The panels now
  pair one to one.
- Web UI: a job that failed before the build started left the page spinning. Each job now
  ends with a status.

## [2.0.0] - 2026-07-09

### Changed

- `latexdiff-zip.py` is the only engine. It is a Python program that uses only the standard
  library. The CLI options and the outputs are the same as those of the bash script.

### Added

- The figure comparison skips a figure whose file changed but whose pixels did not
  (Matplotlib and Inkscape write a new timestamp into every export).
- The figure comparison supports `\graphicspath`.
- A figure only in one version gets its own page in the appendix.

### Removed

- `latexdiff-zip.sh`, the bash engine. The `v1-bash` tag keeps it.

## [1.0.0] - 2026-07-08

The first release, with the bash engine `latexdiff-zip.sh` (tag `v1-bash`).

### Added

- One track-changes PDF from two Overleaf `.zip` exports, with OLD/NEW collages of the
  changed figures after the text diff.
- A container image with all dependencies (`latexdiff-zip-podman.sh`).
- A drag-and-drop web UI (`latexdiff-zip-web.sh`) with a live build log and dark mode.
- Each side finds its own main `.tex`. `-m` and `-M` choose it when there are several.
- Tar archives (`.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`) as inputs, and arXiv ids and URLs.
- Bibliography changes in BibTeX documents show in the diff. This includes arXiv sources that
  ship a `.bbl` without the `.bib`.
- `latexdiff-zip.py`, a Python port of the engine. The container and the web UI use it.
- `tests/test_parity.py`, a test suite that runs the engine over fixture projects.

[Unreleased]: https://github.com/toftul/latexdiff-zip/compare/v2.3.1...HEAD
[2.3.1]: https://github.com/toftul/latexdiff-zip/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/toftul/latexdiff-zip/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/toftul/latexdiff-zip/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/toftul/latexdiff-zip/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/toftul/latexdiff-zip/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/toftul/latexdiff-zip/releases/tag/v1.0.0
