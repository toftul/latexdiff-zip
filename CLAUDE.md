# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that produces a `latexdiff` track-changes PDF between two LaTeX project archives
(typically Overleaf history `.zip` exports or arXiv `.tar.gz` source downloads), including
side-by-side OLD/NEW comparisons of any changed figures. Each side may be a `.zip` or a tar
archive (`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`), dispatched on the
filename extension, and the two sides may differ. Either side may instead be an arXiv id or
URL (`normalize_arxiv` recognises it; `fetch_arxiv` downloads `arxiv.org/e-print/<id>` via
curl/wget and handles all three e-print shapes: tarball, gzipped single `.tex`, and pdf-only
which errors). It is delivered at three layers, each wrapping the one below:

1. **`latexdiff-zip.sh`** — the engine. A single self-contained bash script; everything
   else just packages or invokes it.
2. **`Containerfile`** + **`latexdiff-zip-podman.sh`** — bundle all dependencies so the
   script runs anywhere via Podman.
3. **`Containerfile.web`** + **`webapp/`** + **`latexdiff-zip-web.sh`** — a drag-and-drop
   web UI whose backend shells out to the CLI inside the container.

## Commands

```sh
# Run the script directly (needs host deps: unzip and/or tar, latexdiff, latexpand,
# pdflatex, bibtex/biber, + optional: ImageMagick, pdfunite/gs, python3)
./latexdiff-zip.sh [-m old-main.tex] [-M new-main.tex] [-o out.pdf] [-t TYPE] [-F] [-c DIR] OLD NEW

# Containerized CLI (builds image on first use; --build forces rebuild)
./latexdiff-zip-podman.sh old.zip new.zip

# Web UI (builds both images on first use; serves on :8080)
./latexdiff-zip-web.sh [PORT]
./latexdiff-zip-web.sh --build      # force rebuild after editing script/app

# Syntax sanity checks
bash -n latexdiff-zip.sh            # shell syntax
python3 -m py_compile webapp/app.py # web app syntax

# Behaviour oracle: runs the engine over tests/cases/ fixtures and asserts on
# the observable contract (exit code, stage/warning log lines, PDF page count,
# pdftotext probes). Green against the bash engine; the Python rewrite is done
# when it is green with LDZ_SCRIPT pointed at the .py.
python3 tests/test_parity.py            # full offline suite (real pdflatex builds)
python3 tests/test_parity.py -k fast    # fast CLI-contract cases only, no LaTeX
LDZ_NETWORK=1 python3 tests/test_parity.py   # + real arXiv-fetch cases
```

`test_for_diff_old.zip`/`test_for_diff_new.zip` in the repo root are real sample fixtures —
use them to smoke-test changes end-to-end. See `tests/README.md` for the oracle's cases and
knobs.

### Testing constraint

Rootless Podman **cannot be built or run from inside some sandboxed sessions** (fails with
"cannot re-exec process to join the existing user namespace"). When that happens, the
container layers can only be validated by the user running the build on their own machine;
the bash engine itself can still be exercised directly on the host.

## Architecture & non-obvious design decisions

**`latexdiff-zip.sh` pipeline:** extract both (`extract_archive` dispatches on extension —
`unzip` for `.zip`, `tar -xf` for tar archives, which auto-detects gzip/bzip2/xz; arXiv
sides are fetched by `fetch_arxiv` instead) → descend
into a single top-level dir if present → auto-detect the main `.tex` (the one with
`\documentclass`) → `latexpand` each into one
flat file → for BibTeX docs, compile each side once + `bibtex` and re-flatten with
`latexpand --expand-bbl` so bibliography changes get diffed → `latexdiff` the two → copy
the **new** project's assets into a build dir → compile → compare figures → embed figure
collages into the PDF.

- **Bibliography expansion is all-or-nothing and has a fallback ladder.** If any side that
  needs a `.bbl` can't produce one, *both* sides stay unexpanded (expanding one side only
  would mark the entire bibliography as changed). If the expanded diff fails to compile,
  `build_diff_pdf` is rerun on a diff of the unexpanded flats. biblatex is deliberately
  skipped: its `.bbl` is driver code, and latexpand's `--biber` embeds it verbatim in a
  `filecontents*` block where latexdiff markup would corrupt it, not display it.

- **Manual pdflatex sequence, deliberately not latexmk.** latexdiff markup routinely makes
  pdflatex exit non-zero (e.g. amsmath "Multiple \label's"), and latexmk's rerun heuristics
  then stop too early, leaving `??` refs. The fixed `pdflatex → bib backend → pdflatex ×2`
  sequence ignores non-zero exits and always converges. Don't "simplify" this back to latexmk
  — there's a long code comment explaining why (commit history shows latexmk was tried and reverted).
- **The diff compiles against the NEW project's assets only.** Figures that existed solely in
  the old version won't be present at compile time.
- **bash 3.2 portability (stock macOS).** No `mapfile`/`readarray`; uses portable
  `while IFS= read` loops instead. Keep new code 3.2-compatible.

**Figure comparison (`compare_figures` and helpers):** all of it degrades gracefully when the
optional deps are missing — never make these hard requirements.

- Figures are matched between versions by **`\label` first, then document order**, via an
  inline `python3` heredoc (`_match_figures_py`). This is what lets a renamed image file
  (`plot_v1.pdf` → `plot_v2.pdf`) still be paired. Without python3 it falls back to
  path-based matching (same filename in both zips).
- `_im`/`_im_identify` wrap ImageMagick, preferring v7 `magick` over v6 `convert`/`identify`.
- Collages are built as PNGs (any source format is rasterised first), then **embedded by
  building a separate standalone appendix PDF and merging it** (`pdfunite`, falling back to
  `gs`) — rather than injecting into the fragile latexdiff source. `-F` disables embedding;
  `-c DIR` also writes the PNGs to a folder.

**Container layering:** `Containerfile` is based on `texlive/texlive` (already has
latexdiff/latexpand/biber) and only adds the figure tooling; it also strips the Debian
ImageMagick `policy.xml` lines that block PDF/PS/EPS (required to rasterise figure PDFs).
`Containerfile.web` is `FROM latexdiff-zip:latest` and **resets `ENTRYPOINT []`** (the CLI
image sets `ENTRYPOINT ["latexdiff-zip"]`) so its `CMD` can launch gunicorn instead.

**Web backend (`webapp/app.py`):** a job model, not a single blocking request.
`POST /jobs` starts the build in a background thread and returns a job id; `GET /jobs/<id>/events`
streams the live log via Server-Sent Events; `GET /jobs/<id>/pdf` serves the result. Job state
(uploads, `log.txt`, `status`, `diff.pdf`) lives on the filesystem under `JOBS_ROOT`. The build
has its own `LDZ_TIMEOUT` watchdog (default 600s). gunicorn therefore runs **one threaded worker
with `--timeout 0`** so the stream, the build, and the download share process state and long
builds don't trip the worker watchdog — keep it single-worker.

## Constraints to respect

- The web UI compiles untrusted uploaded LaTeX (shell-escape is off by default but still a
  code-execution surface). It is intended for trusted/personal use, not public hosting.
- Keep the optional figure-diff dependencies optional everywhere — the CLI must still produce
  a text diff PDF when ImageMagick/python3/pdfunite are absent.
