#!/usr/bin/env bash
# latexdiff-zip: produce a latexdiff PDF between two LaTeX project zips
# (e.g. two snapshots downloaded from Overleaf history).
#
# Usage: latexdiff-zip [-m main.tex] [-o output.pdf] [-t TYPE] old.zip new.zip
#
# The only artifact left behind is the diff PDF (default: alongside new.zip
# as diff.pdf). All intermediate files are produced in a temp directory
# and cleaned up on exit.

set -euo pipefail

main_tex=""
out_pdf=""
diff_type="UNDERLINE"

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [-m main.tex] [-o output.pdf] [-t TYPE] old.zip new.zip

  -m main.tex    Main .tex file (relative to project root). Auto-detected if omitted.
  -o output.pdf  Output PDF path (default: <dir of new.zip>/diff.pdf).
  -t TYPE        latexdiff --type value (default: UNDERLINE).
EOF
    exit 2
}

while getopts ":m:o:t:h" opt; do
    case "$opt" in
        m) main_tex="$OPTARG" ;;
        o) out_pdf="$OPTARG" ;;
        t) diff_type="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))

[[ $# -eq 2 ]] || usage
old_zip="$1"
new_zip="$2"

for f in "$old_zip" "$new_zip"; do
    [[ -f "$f" ]] || { echo "error: not a file: $f" >&2; exit 1; }
done

for cmd in unzip latexdiff latexpand pdflatex; do
    command -v "$cmd" >/dev/null || { echo "error: missing dependency: $cmd" >&2; exit 1; }
done

# Resolve paths before we cd anywhere.
old_zip_abs="$(cd "$(dirname "$old_zip")" && pwd)/$(basename "$old_zip")"
new_zip_abs="$(cd "$(dirname "$new_zip")" && pwd)/$(basename "$new_zip")"
if [[ -z "$out_pdf" ]]; then
    out_pdf="$(dirname "$new_zip_abs")/diff.pdf"
fi
mkdir -p "$(dirname "$out_pdf")"
out_pdf="$(cd "$(dirname "$out_pdf")" && pwd)/$(basename "$out_pdf")"

tmp="$(mktemp -d -t latexdiff-zip.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/old" "$tmp/new" "$tmp/build"
unzip -q "$old_zip_abs" -d "$tmp/old"
unzip -q "$new_zip_abs" -d "$tmp/new"

# If the zip contained a single top-level directory, descend into it.
descend_single_root() {
    local d="$1"
    local entries=()
    # Portable read loop (works on bash 3.2, e.g. stock macOS) in place of mapfile.
    while IFS= read -r entry; do entries+=("$entry"); done \
        < <(find "$d" -mindepth 1 -maxdepth 1)
    if [[ ${#entries[@]} -eq 1 && -d "${entries[0]}" ]]; then
        echo "${entries[0]}"
    else
        echo "$d"
    fi
}
old_root="$(descend_single_root "$tmp/old")"
new_root="$(descend_single_root "$tmp/new")"

detect_main() {
    local root="$1"
    local found=()
    while IFS= read -r f; do found+=("$f"); done \
        < <(grep -l -E '^\s*\\documentclass' "$root"/*.tex 2>/dev/null || true)
    if [[ ${#found[@]} -eq 1 ]]; then
        basename "${found[0]}"
    elif [[ ${#found[@]} -gt 1 ]]; then
        echo "error: multiple candidate main .tex files in $root:" >&2
        printf '  %s\n' "${found[@]}" >&2
        echo "  pass -m <filename> to choose one." >&2
        exit 1
    else
        echo "error: no .tex file with \\documentclass found in $root" >&2
        exit 1
    fi
}

if [[ -z "$main_tex" ]]; then
    main_old="$(detect_main "$old_root")"
    main_new="$(detect_main "$new_root")"
    if [[ "$main_old" != "$main_new" ]]; then
        echo "error: main file differs between zips ($main_old vs $main_new); pass -m." >&2
        exit 1
    fi
    main_tex="$main_new"
fi

[[ -f "$old_root/$main_tex" ]] || { echo "error: $main_tex not found in old zip root ($old_root)" >&2; exit 1; }
[[ -f "$new_root/$main_tex" ]] || { echo "error: $main_tex not found in new zip root ($new_root)" >&2; exit 1; }

echo "main file: $main_tex"
echo "flattening..."
( cd "$old_root" && latexpand --keep-comments "$main_tex" > "$tmp/old_flat.tex" 2>/dev/null )
( cd "$new_root" && latexpand --keep-comments "$main_tex" > "$tmp/new_flat.tex" 2>/dev/null )

echo "running latexdiff (type=$diff_type)..."
set +e
latexdiff --type="$diff_type" --append-safecmd="label" \
    "$tmp/old_flat.tex" "$tmp/new_flat.tex" > "$tmp/build/diff.tex" 2> "$tmp/latexdiff.log"
ld_status=$?
set -e
if [[ ! -s "$tmp/build/diff.tex" ]]; then
    echo "latexdiff failed (status $ld_status). Log:" >&2
    cat "$tmp/latexdiff.log" >&2
    exit 1
fi

# Copy every non-main file from the new project into the build dir so figures,
# .bib, .cls, .sty and the like resolve during compilation.
( cd "$new_root" && find . -type f ! -path "./$main_tex" -print0 | \
    while IFS= read -r -d '' f; do
        rel="${f#./}"
        mkdir -p "$tmp/build/$(dirname "$rel")"
        cp "$f" "$tmp/build/$rel"
    done )

echo "building PDF..."
cd "$tmp/build"

# We drive the toolchain by hand rather than via latexmk. latexdiff markup
# routinely makes pdflatex exit non-zero (e.g. amsmath "Multiple \label's" when
# a labelled equation is edited), and latexmk's rerun heuristics treat that as a
# reason to stop after too few passes -- leaving every \ref as "??" and every
# \cite undefined. The classic fixed sequence below always converges: the PDF
# pdflatex emits despite those errors is still usable, so each pass ignores a
# non-zero exit.
runtex() { pdflatex -interaction=nonstopmode diff.tex >/dev/null 2>&1 || true; }

runtex   # pass 1: write .aux (labels and \citation entries)

# Run whichever bibliography backend the document actually uses.
if [[ -f diff.bcf ]]; then
    if command -v biber >/dev/null; then
        biber diff >/dev/null 2>&1 || true
    else
        echo "warning: document needs biber but it is not installed; citations may stay unresolved" >&2
    fi
elif grep -q '\\bibdata' diff.aux 2>/dev/null; then
    if command -v bibtex >/dev/null; then
        bibtex diff >/dev/null 2>&1 || true
    else
        echo "warning: document needs bibtex but it is not installed; citations may stay unresolved" >&2
    fi
fi

runtex   # pass 2: pull in the .bbl and resolve labels (writes \bibcite entries)
runtex   # pass 3: read \bibcite back so citations and cross-references converge

if [[ ! -f diff.pdf ]]; then
    echo "error: PDF build failed. Tail of diff.log:" >&2
    tail -40 diff.log >&2 2>/dev/null || true
    exit 1
fi

cp diff.pdf "$out_pdf"
echo "wrote: $out_pdf"
