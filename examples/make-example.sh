#!/usr/bin/env bash
# Regenerate examples/diff_example.png, the picture in the README and on the web
# page, from the Dirac fixture in tests/cases/dirac-diff.
#
# Usage: examples/make-example.sh [--build]
#
# The script runs the working-tree latexdiff-zip.py inside the latexdiff-zip
# image, so the picture shows the current design even when the image is older.
# The image is built on first use (or with --build). Only podman is needed on
# the host: the zip, the render and the composition all happen in the container.

set -euo pipefail

IMAGE="${LATEXDIFF_ZIP_IMAGE:-latexdiff-zip:latest}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v podman >/dev/null || { echo "error: podman not found on PATH" >&2; exit 1; }

if [[ "${1:-}" == "--build" ]]; then
    podman build -t "$IMAGE" "$repo"
elif ! podman image exists "$IMAGE"; then
    echo "image '$IMAGE' not found; building it (first run only)..." >&2
    podman build -t "$IMAGE" "$repo"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The repo is mounted read-only. label=disable instead of :Z, so SELinux labels
# on the working tree stay as they are.
podman run --rm --userns=keep-id --security-opt label=disable \
    -v "$repo":/repo:ro -v "$work":/work -w /work \
    --entrypoint bash "$IMAGE" -euc '
python3 - <<"EOF"
import os, zipfile
for side, name in (("old", "dirac-v1.zip"), ("new", "dirac-v2.zip")):
    src = os.path.join("/repo/tests/cases/dirac-diff", side)
    with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in sorted(os.listdir(src)):
            zf.write(os.path.join(src, fn), fn)
EOF
python3 /repo/latexdiff-zip.py -o diff.pdf -c figs dirac-v1.zip dirac-v2.zip
pdftoppm -r 200 -f 1 -l 1 -png -singlefile diff.pdf page

# The text diff (page 1) on the left and the figure comparison (the -c PNG) on
# the right, scaled to one height and joined edge to edge on white: both
# already have white margins, so the result is one seamless white picture.
h=780
magick page.png -resize "x$h" left.png
magick figs/fig_man_diff.png -resize "x$h" right.png
magick -background white left.png right.png -gravity center +append \
    -resize 1600x -depth 8 -strip diff_example.png
'

cp "$work/diff_example.png" "$repo/examples/diff_example.png"
echo "wrote: $repo/examples/diff_example.png"
