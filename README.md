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
PDF**. A live status shows the current build stage (flattening, latexdiff, the PDF
passes, figure comparison), with the full log one click away under *Show build
log*. When it finishes the diff PDF is previewed inline, with **Download** /
**Open in new tab** links and a **Start over** button.

There's also a light/dark theme toggle, and your markup-style and figure-embedding
choices are remembered between visits.

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
> is off by default, but it is still a code-execution surface, so only run it for
> projects you trust, or isolate it as described below.

### Hosting it publicly (Cloudflare Tunnel)

To reach the web UI from anywhere — even with **no public IP** — see
[`DEPLOY.md`](DEPLOY.md). It walks through exposing it at your own domain through a
**Cloudflare Tunnel**, with the container running in a dedicated VM. Because it runs
untrusted LaTeX with no login, follow the security notes there: keep it in a throwaway
VM, block the VM's access to the rest of your LAN, rate-limit at the edge, and patch
the image regularly.

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

## Diffing directly in Overleaf (no download)

If you'd rather stay inside Overleaf, you can run `latexdiff` there without exporting any
zips. See Overleaf's own guide:
<https://www.overleaf.com/learn/latex/Articles/How_to_use_latexdiff_on_Overleaf>.

The method I like: keep the old version of your main file in the project (e.g.
`monoclinic_CD_first_submit.tex`) next to the current one (`monoclinic_CD.tex`), add a
`diff.tex` file with the content below, and compile **`diff.tex`** as the main document. It
shells out to `latexdiff` at build time and `\input`s the result:

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

Set `\oldFile` / `\newFile` to your two filenames. This relies on shell-escape, which
Overleaf enables by default. Compared with `latexdiff-zip`, it diffs a single `.tex` file
rather than a whole flattened project, and produces no figure collages — but it needs no
local tools and no downloads.

## License

MIT
