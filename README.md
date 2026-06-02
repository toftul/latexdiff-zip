# latexdiff-zip

Produce a [`latexdiff`](https://ctan.org/pkg/latexdiff) PDF between two zipped LaTeX
projects. For example, two snapshots downloaded from an
[Overleaf](https://www.overleaf.com/) project's history.

Usage. Download and make executable:
```sh
curl -O https://raw.githubusercontent.com/toftul/latexdiff-zip/master/latexdiff-zip.sh
chmod +x latexdiff-zip.sh
```
Run:
```sh
./latexdiff-zip.sh [-m main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] old.zip new.zip
```

## Why

Overleaf's built-in history is great for browsing, but it can't hand you a single
compiled PDF that shows every change between two arbitrary versions. If you keep
the `.zip` exports (Menu → Download, or from the history view), this script turns
any two of them into a track-changes-style diff PDF.

## Requirements

The following commands must be on your `PATH`:

- `unzip`
- `latexdiff`
- `latexpand`
- `pdflatex`
- `bibtex` (or `biber`, for biblatex documents)

Optional, for the figure-diff feature (gracefully skipped if missing):

- `ImageMagick` (`magick` or `convert`/`identify`) — builds the OLD/NEW collages.
- `pdfunite` (from poppler) or `gs` (Ghostscript) — appends the collages to the PDF.
- `python3` — matches figures by `\label` so renamed image files are still paired.


| Option | Description |
| --- | --- |
| `-m main.tex` | Main `.tex` file, relative to the project root. Auto-detected if omitted. |
| `-o output.pdf` | Output PDF path. Defaults to `diff.pdf` next to `new.zip`. |
| `-t TYPE` | `latexdiff --type` value (`UNDERLINE`, `CFONT`, `CCHANGEBAR`, …). Default: `UNDERLINE`. |
| `-F` | Do **not** append figure-diff collages to the PDF (they are appended by default). |
| `-c DIR` | Also save the figure-diff collage PNGs into `DIR`. |
| `-h` | Show help. |

## Figure comparison

Beyond the text diff, the script also compares figures. For every figure that
changed between the two versions it builds a side-by-side collage with red
**OLD** / green **NEW** banners, and — by default — appends those collages as
extra pages at the end of the diff PDF.

Figures are paired by their `\label` (parsed from the flattened source), so a
figure still matches even if its image file was **renamed** (e.g.
`plot_v1.pdf` → `plot_v2.pdf`); unlabelled figures fall back to matching by
document order. Source images in any common format (`pdf`, `eps`, `png`, `jpg`,
…) are rasterised to PNG before being composed.

- Pass `-F` to skip embedding the collages in the PDF.
- Pass `-c DIR` to also (or instead) write the collage PNGs into a folder.

### Examples

Diff two Overleaf exports, writing `diff.pdf` beside the new one:

```sh
./latexdiff-zip.sh old.zip new.zip
```

Choose the main file and output path explicitly:

```sh
./latexdiff-zip.sh -m paper.tex -o ~/Desktop/changes.pdf v1.zip v2.zip
```

Use a different markup style:

```sh
./latexdiff-zip.sh -t CCHANGEBAR old.zip new.zip
```

Skip the figure collages in the PDF, but save them as PNGs in a folder:

```sh
./latexdiff-zip.sh -F -c changed_figures old.zip new.zip
```

### Install (optional)

To run it as `latexdiff-zip` from anywhere:

```sh
chmod +x latexdiff-zip.sh
sudo cp latexdiff-zip.sh /usr/local/bin/latexdiff-zip
```

## Run in a container (no host dependencies)

If you'd rather not install TeX Live, ImageMagick and friends, use the bundled
[`Containerfile`](Containerfile). The included wrapper builds the image on first
use and runs everything inside the container:

```sh
./latexdiff-zip-podman.sh old.zip new.zip
./latexdiff-zip-podman.sh -t CCHANGEBAR -c changed_figures old.zip new.zip
```

The wrapper mounts the current directory into the container, runs as your own
user (so output files are owned by you, not root), and accepts all the same
options as the script. Pass `--build` as the first argument to force a rebuild
after changing the script.

Prefer raw Podman/Docker? Build and run directly:

```sh
podman build -t latexdiff-zip .
podman run --rm --userns=keep-id -v "$PWD":/work:Z latexdiff-zip old.zip new.zip
```

(`docker build`/`docker run` work too; drop `--userns=keep-id` and `:Z` for
Docker.) The image is large because it is based on the official `texlive/texlive`
image, but it guarantees every dependency — including the optional figure-diff
tooling — is present.

## Web interface

Prefer clicking to typing? There's a tiny drag-and-drop web UI. It runs the same
container as its backend, so it needs no host dependencies beyond Podman.

```sh
./latexdiff-zip-web.sh          # builds the images on first run, then serves
# open http://localhost:8080
```

Drop the **OLD** and **NEW** project zips, optionally tweak the markup style /
main file / figure embedding under *Advanced options*, and click **Generate diff
PDF**. The build log streams live to the page (so you can see flattening,
latexdiff, the PDF passes and figure comparison as they happen); when it finishes
the diff PDF is previewed inline with a download link.

Useful variants:

```sh
./latexdiff-zip-web.sh 9000     # serve on a different port
./latexdiff-zip-web.sh --build  # force a rebuild after editing the script/app
```

Under the hood [`Containerfile.web`](Containerfile.web) layers a small Flask app
(served by gunicorn) on top of the CLI image; the app runs `latexdiff-zip` as a
background job and streams its output to the browser via Server-Sent Events. Each
build is capped at 10 minutes by default — raise it by passing `-e LDZ_TIMEOUT=1200`
to `podman run`, or by editing the wrapper.

> **Note:** the interface compiles uploaded LaTeX with `pdflatex`. Shell-escape
> is off by default, but you should still only run it for projects you trust
> (e.g. your own Overleaf exports), or behind your own network, not as a public
> service.

## How it works

1. Unzips both archives into a temp directory; if an archive contains a single
   top-level folder, it descends into it.
2. Detects the main file by looking for the `.tex` containing `\documentclass`
   (override with `-m` if there is more than one candidate).
3. Flattens each project into a single file with `latexpand --keep-comments`.
4. Runs `latexdiff --type=<TYPE> --append-safecmd=label` on the two flattened files.
5. Copies all non-main files (figures, `.bib`, `.cls`, `.sty`, …) from the **new**
   project alongside the diff so it compiles.
6. Builds the PDF with `pdflatex` → `bibtex` → `pdflatex` ×2 to resolve
   references and citations.
7. Compares the figures referenced in both versions, builds OLD/NEW collages for
   the changed ones, and appends them to the PDF (unless `-F`).

## Notes & limitations

- The diff is compiled against the **new** project's assets (figures, styles,
  bibliography). Figures that existed only in the old version won't be present.
- `pdflatex` is run without `-halt-on-error`: `latexdiff` markup frequently trips
  minor errors, so the build pushes through them. If no PDF is produced, the tail
  of `diff.log` is printed to help diagnose.
- Both projects must share the same main filename, or you'll need to pass `-m`.
- Engine is fixed to `pdflatex`; XeLaTeX/LuaLaTeX projects aren't supported yet.

## License

MIT
