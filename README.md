# latexdiff-zip

**latexdiff-zip** makes one track-changes PDF from two versions of a LaTeX project. It compares
the text, figures, and bibliography. Each version can be an Overleaf `.zip` export, an arXiv
`.tar.gz` source, an arXiv ID, a single `.tex` file, or a project folder.

I host a copy that you can use now: </br>
**<https://latexdiff.toftul.net/>**

![Example diff PDF: on the left, old text in red strikethrough and new text underlined in blue; on the right, a figure comparison headed Changed, Figure 1, with the OLD and NEW photos labeled side by side](examples/diff_example_readme.png)

## Run it yourself

### Command line

Needs $\LaTeX$ and `python`.

1. Download the script:

   ```sh
   curl -O https://raw.githubusercontent.com/toftul/latexdiff-zip/main/latexdiff-zip.py
   chmod +x latexdiff-zip.py
   ```

2. Run it on two archives, two arXiv IDs, two `.tex` files, or two folders:

   ```sh
   ./latexdiff-zip.py old.zip new.zip
   ./latexdiff-zip.py 1706.03762v1 1706.03762v2   # from arXiv
   ./latexdiff-zip.py old.tex new.tex             # figures and .bib next to them are used
   ./latexdiff-zip.py v1/ v2/
   ```

The script writes `diff.pdf`. [USAGE.md](USAGE.md) lists all options and gives more
examples.

### Command line in a container

Clone the repository, then run:

```sh
./latexdiff-zip-podman.sh old.zip new.zip
```

The script builds the container image on the first run. Keep both inputs in the current
directory, because the container sees only that directory.

### Web UI

Clone the repository, then run:

```sh
./latexdiff-zip-web.sh        # builds on the first run, then serves http://localhost:8080
```

## It is possible to make diff in Overleaf

You can also run vanilla `latexdiff` inside Overleaf. [OVERLEAF.md](OVERLEAF.md) shows how.

## Versions

See [CHANGELOG.md](CHANGELOG.md) for update information.

## Docs

- [**Usage and options**](USAGE.md): every option, how the tool works, and its limits.
- [**Figure comparison**](FIGURES.md): how the tool pairs and compares figures.
- [**Self-hosting**](DEPLOY.md): run your own public copy behind a Cloudflare Tunnel.
- [**Changelog**](CHANGELOG.md): what changed in each version.

## Similar projects

- [**git-latexdiff-web**](https://github.com/am009/git-latexdiff-web): a web page that
  compares two Overleaf `.zip` exports in the browser and returns a PDF. It does not compare
  figures.
- [**comparxiv**](https://github.com/temken/comparxiv): a command-line tool that compares two
  arXiv versions. It needs a local TeX installation. Figure comparison is on its to-do list.
- [**3142.nl/latex-diff**](https://3142.nl/latex-diff/): a web page that runs `latexdiff` on
  two uploaded files. It returns `diff.tex`, not a PDF.

This project is different in three ways. It shows both versions of each changed figure in an
appendix of the PDF. It reads Overleaf `.zip` exports, tar archives, arXiv IDs, `.tex` files,
and folders through one interface. It also compares the bibliography, not only the body text.

## Feedback

To report a bug or suggest an idea, [open an issue](https://github.com/toftul/latexdiff-zip/issues). 🐮

## License

MIT
