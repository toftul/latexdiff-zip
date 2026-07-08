# Behaviour oracle (`test_parity.py`)

An executable definition of what "the engine behaves correctly" means, pinned
to the observable contract rather than the implementation. It runs the engine
under test over the fixture projects in `cases/` and asserts on: exit code, the
stage/warning log lines the web UI and users rely on, whether a PDF was
produced, its page count, and probe strings in the extracted text.

It was built for the Python rewrite of `latexdiff-zip.sh`: both engines pass it,
so it doubles as the regression guard for either. It runs the Python engine by
default; point `LDZ_SCRIPT` at the bash script to confirm the two still agree.

## Running

```sh
python3 tests/test_parity.py            # against ./latexdiff-zip.py (default)
python3 tests/test_parity.py -k fast    # only the fast CLI-contract cases
python3 -m unittest tests.test_parity   # via unittest discovery
LDZ_SCRIPT=./latexdiff-zip.sh python3 tests/test_parity.py   # check bash parity
```

Environment knobs:

| Var | Default | Meaning |
| --- | --- | --- |
| `LDZ_SCRIPT` | `./latexdiff-zip.py` | Engine under test. A `.py` target is run through the current interpreter. |
| `LDZ_NETWORK` | unset | Set to `1` to also run the arXiv-fetch cases (real downloads). |
| `LDZ_CASE_TIMEOUT` | `300` | Per-case timeout in seconds. |

The full cases each drive a real `pdflatex` build (needs the same tools as the
CLI: latexdiff, latexpand, pdflatex, bibtex; plus poppler's `pdfinfo`/`pdftotext`
for the page-count and probe assertions, which are skipped if absent). The
`fast` cases are pure CLI-contract checks with no LaTeX run.

## Fixtures (`cases/`)

Each case is a plain source tree, zipped into a temp archive at run time (via
stdlib `zipfile` — no `zip` binary needed), so the same suite exercises the
extract path of either engine.

| Case | What it locks |
| --- | --- |
| `article-nobib` | Baseline: no bibliography, a changed figure. |
| `bibtex-regen` | BibTeX `.bbl` regenerated from `.bib`; edited/added/removed entries diffed. |
| `natbib` | Same, with `natbib` + `plainnat` (`\bibitem[label]{key}` form). |
| `biblatex` | biblatex is skipped with a note (its `.bbl` is not diffable text). |
| `bib-missing` | A side that can't produce a `.bbl` falls back gracefully with a warning. |
| `revtex-arxiv-bbl-nobib` | arXiv-style source that ships a `.bbl` but no `.bib`, REVTeX `\href` stripping, different old/new main names. Offline repro of the 2406.11300 bug. |
| `same-archive` | One archive holding both `old.tex` and `new.tex`, named via `-M`. |

Fixtures hold only source (`.tex`/`.bib`, and a shipped `.bbl` where that is the
scenario) — never build artifacts. The engine extracts and builds in a temp
dir, so runs never dirty the fixtures.

The two network-only cases (`arxiv_natbib`, `arxiv_oldstyle_selfdiff`) take
arXiv ids directly and are not stored as fixtures.
