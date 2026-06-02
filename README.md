# latexdiff-zip

Produce a [`latexdiff`](https://ctan.org/pkg/latexdiff) PDF between two zipped LaTeX
projects. For example, two snapshots downloaded from an
[Overleaf](https://www.overleaf.com/) project's history.

Usage
```sh
./latexdiff-zip.sh [-m main.tex] [-o output.pdf] [-t TYPE] old.zip new.zip
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
- `bibtex`


| Option | Description |
| --- | --- |
| `-m main.tex` | Main `.tex` file, relative to the project root. Auto-detected if omitted. |
| `-o output.pdf` | Output PDF path. Defaults to `diff.pdf` next to `new.zip`. |
| `-t TYPE` | `latexdiff --type` value (`UNDERLINE`, `CFONT`, `CCHANGEBAR`, …). Default: `UNDERLINE`. |
| `-h` | Show help. |

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

### Install (optional)

To run it as `latexdiff-zip` from anywhere:

```sh
chmod +x latexdiff-zip.sh
sudo cp latexdiff-zip.sh /usr/local/bin/latexdiff-zip
```

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
