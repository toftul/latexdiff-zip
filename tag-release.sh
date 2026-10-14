#!/usr/bin/env bash
# Tag the current commit as a release. The tag name comes from __version__ in
# latexdiff-zip.py, the one place the version is set, so the two cannot differ.
#
# Usage: ./tag-release.sh
#
# Before you run it: bump __version__, move the [Unreleased] entries in
# CHANGELOG.md under "## [X.Y.Z] - YYYY-MM-DD", and commit. The script refuses
# a dirty working tree, a version with no changelog entry, and an existing tag.
# The tag message is the changelog section, so `gh release create vX.Y.Z
# --notes-from-tag` can reuse it later. Nothing is pushed.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

version="$(python3 latexdiff-zip.py -V | awk '{print $2}')"
tag="v$version"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: commit or stash your changes first; a tag names a commit" >&2
    exit 1
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    echo "error: tag $tag already exists (on $(git rev-list -n1 --abbrev-commit "$tag"))" >&2
    echo "       bump __version__ in latexdiff-zip.py for a new release" >&2
    exit 1
fi

# The changelog section: from its "## [X.Y.Z] - date" heading up to the next
# "## [" heading or the link references at the bottom.
notes="$(awk -v v="$version" '
    index($0, "## [" v "] - ") == 1 { on = 1; next }
    on && (/^## \[/ || /^\[[^]]+\]: /) { exit }
    on { print }
' CHANGELOG.md)"
if [[ -z "${notes//[[:space:]]/}" ]]; then
    echo "error: CHANGELOG.md has no \"## [$version] - YYYY-MM-DD\" section" >&2
    exit 1
fi

# --cleanup=verbatim: the default would drop the "### Added" lines as comments.
git tag -a "$tag" --cleanup=verbatim -F - <<EOF
latexdiff-zip $version
$notes
EOF

echo "tagged $tag on $(git rev-parse --short HEAD)"
echo "publish with: git push origin main $tag"
