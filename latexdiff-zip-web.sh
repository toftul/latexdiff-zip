#!/usr/bin/env bash
# Build (if needed) and launch the latexdiff-zip web interface with Podman.
#
# Usage: ./latexdiff-zip-web.sh [PORT]   (default port 8080)
#        ./latexdiff-zip-web.sh --build  (force rebuild of both images)

set -euo pipefail

CLI_IMAGE="${LATEXDIFF_ZIP_IMAGE:-latexdiff-zip:latest}"
WEB_IMAGE="${LATEXDIFF_ZIP_WEB_IMAGE:-latexdiff-zip-web:latest}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
force_build=0

command -v podman >/dev/null || { echo "error: podman not found on PATH" >&2; exit 1; }

if [[ "${1:-}" == "--build" ]]; then force_build=1; shift; fi
port="${1:-8080}"

if [[ $force_build -eq 1 ]] || ! podman image exists "$CLI_IMAGE"; then
    echo "building $CLI_IMAGE ..." >&2
    podman build -t "$CLI_IMAGE" -f "$script_dir/Containerfile" "$script_dir"
fi
if [[ $force_build -eq 1 ]] || ! podman image exists "$WEB_IMAGE"; then
    echo "building $WEB_IMAGE ..." >&2
    podman build -t "$WEB_IMAGE" -f "$script_dir/Containerfile.web" "$script_dir"
fi

echo
echo "latexdiff-zip web interface running at:  http://localhost:${port}"
echo "press Ctrl-C to stop."
echo
exec podman run --rm -p "${port}:8080" "$WEB_IMAGE"
