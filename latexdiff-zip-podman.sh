#!/usr/bin/env bash
# Run latexdiff-zip inside a Podman container so no host dependencies are needed.
#
# Usage: ./latexdiff-zip-podman.sh [latexdiff-zip options] old.zip new.zip
#
# All path arguments must live under the current directory (it is mounted into
# the container at /work). Output is written back into the current directory and
# owned by you. The image is built automatically the first time it is needed.

set -euo pipefail

IMAGE="${LATEXDIFF_ZIP_IMAGE:-latexdiff-zip:latest}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v podman >/dev/null || { echo "error: podman not found on PATH" >&2; exit 1; }

# Build the image once if it is not already present (or force with --build).
if [[ "${1:-}" == "--build" ]]; then
    shift
    podman build -t "$IMAGE" "$script_dir"
elif ! podman image exists "$IMAGE"; then
    echo "image '$IMAGE' not found; building it (first run only)..." >&2
    podman build -t "$IMAGE" "$script_dir"
fi

# Mount the current directory as /work and run as the calling user so the diff
# PDF and any collage folder come out owned by you rather than root.
exec podman run --rm \
    --userns=keep-id \
    -v "$PWD":/work:Z \
    -w /work \
    "$IMAGE" "$@"
