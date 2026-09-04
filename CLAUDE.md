# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that produces a `latexdiff` track-changes PDF between two LaTeX project archives
(typically Overleaf history `.zip` exports or arXiv `.tar.gz` source downloads), including
side-by-side OLD/NEW comparisons of any changed figures. Each side may be a `.zip` or a tar
archive (`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`), dispatched on the
filename extension, and the two sides may differ. Either side may instead be an arXiv id or
URL (`normalize_arxiv` recognises it; `fetch_arxiv` downloads `arxiv.org/e-print/<id>` and
handles all three e-print shapes: tarball, gzipped single `.tex`, and pdf-only which errors).
It is delivered at three layers, each wrapping the one below:

1. **`latexdiff-zip.py`** — the engine. A single self-contained, **stdlib-only** Python
   program (unpacks archives with `zipfile`/`tarfile`, fetches arXiv over HTTPS with
   `urllib`); everything else just packages or invokes it. (It was originally a bash script,
   `latexdiff-zip.sh`, removed once the port reached parity; the `v1-bash` tag preserves it.)
2. **`Containerfile`** + **`latexdiff-zip-podman.sh`** — bundle all dependencies so the
   engine runs anywhere via Podman. The `Containerfile` COPYs `latexdiff-zip.py` to
   `/usr/local/bin/latexdiff-zip`.
3. **`Containerfile.web`** + **`webapp/`** + **`latexdiff-zip-web.sh`** — a drag-and-drop
   web UI whose backend shells out to the CLI inside the container. `webapp/app.py` imports
   `normalize_arxiv` from the engine (one implementation, no mirrored copy).

## Commands

```sh
# Run the engine directly (needs host deps: python3, latexdiff, latexpand,
# pdflatex, bibtex/biber, + optional: ImageMagick, pdfunite/gs)
./latexdiff-zip.py [-m old-main.tex] [-M new-main.tex] [-o out.pdf] [-t TYPE] [-F] [-c DIR] OLD NEW

# Containerized CLI (builds image on first use; --build forces rebuild)
./latexdiff-zip-podman.sh old.zip new.zip

# Web UI (builds both images on first use; serves on :8080)
./latexdiff-zip-web.sh [PORT]
./latexdiff-zip-web.sh --build      # force rebuild after editing script/app

# Syntax sanity checks
python3 -m py_compile latexdiff-zip.py  # engine syntax
python3 -m py_compile webapp/app.py webapp/mains.py   # web app syntax

# Web-only unit tests for the main-.tex scan (no Flask, no LaTeX, ~1s). Includes
# the drift guard tying webapp/mains.py to the engine's detect_main.
python3 tests/test_mains.py

# Web backend: every build ends in a DONE/FAIL status (so the SSE stream can't
# leave the page spinning), plus the staged-upload contract POST /jobs resolves
# its two sides from. Flask is stubbed; instant.
python3 tests/test_webjob.py

# Behaviour oracle: runs the engine over tests/cases/ fixtures and asserts on
# the observable contract (exit code, stage/warning log lines, PDF page count,
# pdftotext probes). Runs latexdiff-zip.py by default; LDZ_SCRIPT can point it
# at any other engine build.
python3 tests/test_parity.py            # full offline suite (real pdflatex builds)
python3 tests/test_parity.py -k fast    # fast CLI-contract cases only, no LaTeX
LDZ_NETWORK=1 python3 tests/test_parity.py       # + real arXiv-fetch cases
```

`test_for_diff_old.zip`/`test_for_diff_new.zip` in the repo root are real sample fixtures —
use them to smoke-test changes end-to-end. See `tests/README.md` for the oracle's cases and
knobs.

### Testing constraint

Rootless Podman **cannot be built or run from inside some sandboxed sessions** (fails with
"cannot re-exec process to join the existing user namespace"). When that happens, the
container layers can only be validated by the user running the build on their own machine;
the engine itself can still be exercised directly on the host.

## Architecture & non-obvious design decisions

**`latexdiff-zip.py` pipeline:** extract both (`extract_archive` dispatches on extension —
`zipfile` for `.zip`, `tarfile` for tar archives, which auto-detects gzip/bzip2/xz; arXiv
sides are fetched by `fetch_arxiv` over HTTPS with `urllib` instead) → descend
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
- **The diff compiles against the NEW project's assets, plus old-only figures.** latexdiff
  runs with `--graphics-markup=both` so a deleted `\includegraphics` stays live (drawn at
  half scale, crossed out) instead of being commented into a `%DIFDELCMD` line — the default
  `new-only` level makes a dropped figure vanish from the diff entirely. `copy_old_figures`
  then fills in every old reference the build dir cannot already resolve, filed under the
  path the deleted command asks for; a NEW asset of the same name always wins. The build
  ladder has a third rung for this: if the deleted-figure markup makes a document unbuildable
  (latexdiff's manual warns about "Misplaced \noalign" in tables), the diff is redone without
  it.
- **Stdlib only, no pip deps.** The engine imports nothing outside the standard library; keep
  it that way. The LaTeX toolchain and the figure tooling (ImageMagick, pdfunite/gs) are the
  only external programs, all invoked via `subprocess`.

**Figure comparison (`compare_figures` and helpers):** all of it degrades gracefully when the
optional deps are missing — never make these hard requirements.

- Figures are matched between versions by **`\label` first, then document order**
  (`match_figures`). This is what lets a renamed image file (`plot_v1.pdf` → `plot_v2.pdf`)
  still be paired. `match_figures` returns three lists — **changed** (paired, an OLD/NEW
  collage), **added** and **removed** (one-sided figures, a single `NEW ONLY`/`OLD ONLY`
  panel via `make_single`). `_resolve` honours `\graphicspath` (document dir first, then each
  entry, so both a bare name and an explicit `figs/plot` resolve).
- **Two-stage change detection avoids false positives.** `match_figures` drops byte-identical
  pairs by md5; then, after rasterising, `compare_figures` drops pairs that are pixel-identical
  via `_visually_identical` (`magick compare -metric AE`). This is what stops a regenerated
  PDF whose only change is a `/CreationDate` from producing a spurious collage.
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

**One upload per archive (`POST /inspect` → staged id → `POST /jobs`).** Picking a file
uploads it *once*, to `/inspect`, which stages it under `UPLOADS_ROOT` and returns an id; the
later `POST /jobs` names the two staged archives and carries no archive bytes (~800 bytes
instead of the sum of both). This is not an optimisation but a correctness fix: sending both
sides in one request made their sizes add up against the **reverse proxy's body cap** — 100 MB
on the Cloudflare free plan the deployment uses — so two 55 MB projects, each fine on its own,
were rejected at the edge with a 413 the app never saw, leaving the page on an empty log. The
browser checks each file against `MAX_UPLOAD_MB` (`LDZ_MAX_UPLOAD_MB`, default 100) before
uploading, and reports upload progress — hence `XMLHttpRequest` rather than `fetch` in
`scanArchive`, since only XHR exposes `upload.onprogress`. Staged files are cleaned up by age
like job dirs, so `POST /jobs` must keep treating an unknown id as "expired, pick the file
again"; posting the archives directly still works as a fallback.

**Main-.tex suggestions (`webapp/mains.py` + `POST /inspect`):** the same `/inspect` call fills
each side's main-.tex dropdown (`candidates()` reads the entry list and the top-level `.tex`
members), so an archive with several `\documentclass` files is resolved by clicking rather than
by a failed build and a typed filename. Two rules to keep:

- **It is not a call into the engine, deliberately.** `detect_main` takes an extracted directory,
  returns exactly one name, and `die()`s on ambiguity; the UI needs an un-extracted archive and
  treats "several" as the answer worth showing. `latexdiff-zip.py` stays untouched by this
  feature — the mirrored rule is pinned instead by `test_agrees_with_engine` in
  `tests/test_mains.py`, which asserts the two agree over every fixture.
- **Never be more permissive than the engine.** Suggesting a file the engine can't then use is
  worse than suggesting nothing, so `candidates()` returns `[]` on anything it can't read and
  the UI silently falls back to auto-detect plus the free-text box ("Other…").

## Constraints to respect

- The web UI compiles untrusted uploaded LaTeX (shell-escape is off by default but still a
  code-execution surface). It is intended for trusted/personal use, not public hosting.
- Keep the optional figure-diff dependencies optional everywhere — the CLI must still produce
  a text diff PDF when ImageMagick/python3/pdfunite are absent.
