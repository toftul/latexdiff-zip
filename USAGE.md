# Usage & options

[← back to README](README.md)

```sh
./latexdiff-zip.sh [-m main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] old.zip new.zip
```

It produces a [`latexdiff`](https://ctan.org/pkg/latexdiff) PDF between two zipped LaTeX
projects — for example, two snapshots from an Overleaf project's history. Overleaf's history
is great for browsing, but it can't hand you a single PDF showing every change between two
arbitrary versions; this does.

## Requirements

These must be on your `PATH`:

- `unzip`
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
| `-m main.tex` | Main `.tex` file, relative to the project root. Auto-detected if omitted. |
| `-o output.pdf` | Output PDF path. Defaults to `diff.pdf` next to `new.zip`. |
| `-t TYPE` | `latexdiff --type` value (`UNDERLINE`, `CFONT`, `CCHANGEBAR`, …). Default: `UNDERLINE`. |
| `-F` | Do **not** append figure-diff collages to the PDF (appended by default). |
| `-c DIR` | Also save the figure-diff collage PNGs into `DIR`. |
| `-h` | Show help. |

## Examples

```sh
# Diff two Overleaf exports, writing diff.pdf beside the new one
./latexdiff-zip.sh old.zip new.zip

# Pick the main file and output path explicitly
./latexdiff-zip.sh -m paper.tex -o ~/Desktop/changes.pdf v1.zip v2.zip

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

1. Unzips both archives; if an archive has a single top-level folder, descends into it.
2. Detects the main file (the `.tex` with `\documentclass`; override with `-m`).
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
- Both projects must share the same main filename, or pass `-m`.
- Engine is fixed to `pdflatex`; XeLaTeX/LuaLaTeX aren't supported yet.

## Diffing directly in Overleaf (no download)

If you'd rather stay inside Overleaf, you can run `latexdiff` there without exporting any
zips. See [Overleaf's guide](https://www.overleaf.com/learn/latex/Articles/How_to_use_latexdiff_on_Overleaf).

The method I like: keep the old version of your main file in the project (e.g.
`monoclinic_CD_first_submit.tex`) next to the current one (`monoclinic_CD.tex`), add a
`diff.tex` file with the content below, and compile **`diff.tex`** as the main document:

```latex
% based on
% https://tex.stackexchange.com/a/603351/249682

\RequirePackage{shellesc}

\newcommand{\oldFile}{monoclinic_CD_first_submit}
\newcommand{\newFile}{monoclinic_CD}

\ShellEscape{latexdiff "\oldFile.tex" "\newFile.tex" > diff_result.tex}

\input{diff_result}
\documentclass{dummy}
```

Set `\oldFile` / `\newFile` to your filenames. It relies on shell-escape (on by default in
Overleaf), diffs a single `.tex` rather than a flattened project, and makes no figure
collages — but needs no local tools.
