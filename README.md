# latexdiff-zip

Turn two zipped LaTeX projects (e.g. Overleaf history exports) into one
track-changes PDF — text *and* figures.

### 👉 I host a ready-to-use copy: **<https://latexdiff-web.toftul.net/>**

Drag in your two `.zip` files, get a diff PDF back. Nothing to install.

## Run it yourself

**Web UI** (drag-and-drop) — clone the repo, then, with [Podman](https://podman.io/) installed:

```sh
./latexdiff-zip-web.sh        # builds on first run → http://localhost:8080
```

**CLI** — needs `latexdiff`, `latexpand`, `pdflatex` & friends on your `PATH`:

```sh
curl -O https://raw.githubusercontent.com/toftul/latexdiff-zip/main/latexdiff-zip.sh
chmod +x latexdiff-zip.sh
./latexdiff-zip.sh old.zip new.zip
```

**CLI, no dependencies** — run it inside the container instead:

```sh
./latexdiff-zip-podman.sh old.zip new.zip
```

## Docs

- [**Usage & options**](USAGE.md) — every CLI flag, how it works, limitations, and a no-download Overleaf trick.
- [**Figure comparison**](FIGURES.md) — how changed figures get paired and diffed.
- [**Self-hosting**](DEPLOY.md) — put your own public copy online with a Cloudflare Tunnel.

## Feedback

Spotted a bug or have an idea? [Open an issue](https://github.com/toftul/latexdiff-zip/issues) — I'd love the feedback. 🙂

## License

MIT
