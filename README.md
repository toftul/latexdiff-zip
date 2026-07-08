# latexdiff-zip

Turn two LaTeX project archives — Overleaf `.zip` history exports or arXiv
`.tar.gz` source — into one track-changes PDF — text ***and*** figures.
Or skip the downloading and just give it two arXiv IDs.

I host a ready-to-use copy: </br>
**<https://latexdiff.toftul.net/>**

Drag in your two archives (`.zip` or `.tar.gz`) — or paste arXiv IDs — and get
a diff PDF back. 

![Example diff PDF: old text struck out in red, new text underlined in blue, and an OLD vs NEW side-by-side figure comparison](examples/diff_example.png)

*Old text struck out in red, new text underlined in blue, and changed figures compared side by side.*

## Run it yourself

### **CLI** — needs $\LaTeX$ installed in the system:

Download script
```sh
curl -O https://raw.githubusercontent.com/toftul/latexdiff-zip/main/latexdiff-zip.sh
chmod +x latexdiff-zip.sh
```
Run
```sh
./latexdiff-zip.sh old.zip new.zip
./latexdiff-zip.sh 1706.03762v1 1706.03762v2   # straight from arXiv
```

See [USAGE.md](USAGE.md) for all CLI flags and more examples. 

### **CLI (container)** — run it inside the container instead:

```sh
./latexdiff-zip-podman.sh old.zip new.zip
```

### **Web UI** (drag-and-drop) — clone the repo, then, with [Podman](https://podman.io/) installed:

```sh
./latexdiff-zip-web.sh        # builds on first run → http://localhost:8080
```

## On Overleaf? Diff right there

You can run `latexdiff` *inside* Overleaf. [Here's the trick.](OVERLEAF.md)

## Docs

- [**Usage & options**](USAGE.md) — every CLI flag, how it works, and limitations.
- [**Figure comparison**](FIGURES.md) — how changed figures get paired and diffed.
- [**Self-hosting**](DEPLOY.md) — put your own public copy online with a Cloudflare Tunnel.

## Feedback

Spotted a bug or have an idea? [Open an issue](https://github.com/toftul/latexdiff-zip/issues) 🐮

## License

MIT
