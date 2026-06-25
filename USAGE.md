# Usage & options

[← back to README](README.md)

```sh
./latexdiff-zip.sh [-m old-main.tex] [-M new-main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] OLD NEW
```

It produces a [`latexdiff`](https://ctan.org/pkg/latexdiff) PDF between two LaTeX project
archives — for example, two snapshots from an Overleaf project's history, or two arXiv source
downloads. Overleaf's history is great for browsing, but it can't hand you a single PDF showing
every change between two arbitrary versions; this does.

`OLD` and `NEW` may each be a `.zip` or a tar archive
(`.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`) — so you can diff an Overleaf
`.zip` export against an arXiv `.tar.gz`, or any combination. The format is picked from the
filename extension.

## Requirements

These must be on your `PATH`:

- `unzip` (for `.zip` archives) and/or `tar` (for tar archives)
- `latexdiff`
- `latexpand`
- `pdflatex`
- `bibtex` (or `biber`, for biblatex documents)

Optional, for the [figure diff](FIGURES.md) (skipped gracefully if missing): `ImageMagick`,
`pdfunite`/`gs`, `python3`.

Don't want to install any of this? Use `./latexdiff-zip-podman.sh` — same options, runs in a
container.

## Options

| Option | Description |
| --- | --- |
| `-m main.tex` | Main `.tex` file of the **old** project, relative to its root. Auto-detected if omitted. |
| `-M main.tex` | Main `.tex` file of the **new** project. Auto-detected if omitted; the two projects may use different names. |
| `-o output.pdf` | Output PDF path. Defaults to `diff.pdf` next to the `NEW` archive. |
| `-t TYPE` | `latexdiff --type` value (`UNDERLINE`, `CFONT`, `CCHANGEBAR`, …). Default: `UNDERLINE`. |
| `-F` | Do **not** append figure-diff collages to the PDF (appended by default). |
| `-c DIR` | Also save the figure-diff collage PNGs into `DIR`. |
| `-h` | Show help. |

## Examples

```sh
# Diff two Overleaf exports, writing diff.pdf beside the new one
./latexdiff-zip.sh old.zip new.zip

# Diff two arXiv source tarballs
./latexdiff-zip.sh v1.tar.gz v2.tar.gz

# Mix formats: an Overleaf .zip against an arXiv .tar.gz
./latexdiff-zip.sh overleaf.zip arxiv.tar.gz

# Pick the main file and output path explicitly
./latexdiff-zip.sh -m paper.tex -o ~/Desktop/changes.pdf v1.zip v2.zip

# The two projects' main files have different names
./latexdiff-zip.sh -m old-main.tex -M new-main.tex v1.zip v2.zip

# Both versions live in ONE archive (e.g. old.tex + new.tex side by side):
# pass the same archive twice and name the main file(s). Naming one side is
# enough — the other is auto-detected by excluding the one you named.
./latexdiff-zip.sh -m old.tex -M new.tex paper.zip paper.zip
./latexdiff-zip.sh -M new.tex paper.zip paper.zip      # -m auto-detects old.tex

# Different markup style
./latexdiff-zip.sh -t CCHANGEBAR old.zip new.zip

# Skip collages in the PDF, but save them as PNGs in a folder
./latexdiff-zip.sh -F -c changed_figures old.zip new.zip
```

## Install (optional)

To run it as `latexdiff-zip` from anywhere:

```sh
chmod +x latexdiff-zip.sh
sudo cp latexdiff-zip.sh /usr/local/bin/latexdiff-zip
```

## Run in a container

If you'd rather not install TeX Live & friends, use the bundled
[`Containerfile`](Containerfile):

```sh
./latexdiff-zip-podman.sh old.zip new.zip
./latexdiff-zip-podman.sh -t CCHANGEBAR -c changed_figures old.zip new.zip
```

The wrapper mounts the current directory, runs as your own user (output files are yours, not
root's), and accepts all the same options. Pass `--build` first to force a rebuild.

Prefer raw Podman/Docker?

```sh
podman build -t latexdiff-zip .
podman run --rm --userns=keep-id -v "$PWD":/work:Z latexdiff-zip old.zip new.zip
```

(`docker build`/`docker run` work too; drop `--userns=keep-id` and `:Z` for Docker.)

## How it works

1. Extracts both archives (`.zip` via `unzip`, tar archives via `tar`); if an archive has a
   single top-level folder, descends into it.
2. Detects each project's main file independently (the `.tex` with `\documentclass`;
   override with `-m` for the old project, `-M` for the new).
3. Flattens each project into one file with `latexpand --keep-comments`.
4. Runs `latexdiff --type=<TYPE> --append-safecmd=label` on the two flattened files.
5. Copies all non-main files (figures, `.bib`, `.cls`, `.sty`, …) from the **new** project
   alongside the diff so it compiles.
6. Builds the PDF with `pdflatex` → `bibtex` → `pdflatex` ×2 to resolve refs and citations.
7. Compares figures and appends OLD/NEW collages for the changed ones (unless `-F`) —
   see [figure comparison](FIGURES.md).

## Notes & limitations

- The diff compiles against the **new** project's assets. Figures that existed only in the
  old version won't be present.
- `pdflatex` runs without `-halt-on-error`: `latexdiff` markup often trips minor errors, so
  the build pushes through them. If no PDF is produced, the tail of `diff.log` is printed.
- Each project's main file is detected independently, so the two may have different
  filenames (e.g. a renamed Overleaf project). If a project has several `\documentclass`
  files, pass `-m` (old) / `-M` (new) to disambiguate.
- When the **same archive** is given for both sides (it holds both an old and a new main
  file), naming just one side with `-m`/`-M` is enough — auto-detection of the other side
  drops the main you named, so it won't trip over the two `\documentclass` files. Name both,
  or at least one; leaving both blank can't tell which version is old vs new and errors.
- Engine is fixed to `pdflatex`; XeLaTeX/LuaLaTeX aren't supported yet.
