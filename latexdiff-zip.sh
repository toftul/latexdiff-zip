#!/usr/bin/env bash
# latexdiff-zip: produce a latexdiff PDF between two LaTeX project archives
# (e.g. two snapshots downloaded from Overleaf history, or arXiv source tarballs).
# Both .zip and tar archives (.tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz)
# are accepted, and the two sides may use different formats.
#
# Usage: latexdiff-zip [-m main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] old.{zip,tar.gz} new.{zip,tar.gz}
#
# Artifacts left behind: the diff PDF (default: alongside the new archive as diff.pdf).
# When ImageMagick is available, every figure that changed between the two archives
# gets a side-by-side OLD/NEW collage. By default the collages are appended as
# extra pages to the diff PDF; pass -F to disable that, and/or -c DIR to also
# save the collage PNGs into a folder. All intermediate files are produced in a
# temp directory and cleaned up on exit.

set -euo pipefail

main_old=""      # -m: main .tex for the OLD project (auto-detected if empty)
main_new=""      # -M: main .tex for the NEW project (auto-detected if empty)
out_pdf=""
diff_type="UNDERLINE"
embed_figs=1     # append figure collages to the diff PDF (default on); -F disables
fig_dir=""       # if set (-c DIR), also write collage PNGs into this folder

# ---- Figure comparison helpers (require ImageMagick) ----------------------

_im() {
    if command -v magick >/dev/null 2>&1; then
        magick "$@"
    else
        convert "$@"
    fi
}

_im_identify() {
    if command -v magick >/dev/null 2>&1; then
        magick identify "$@"
    else
        identify "$@"
    fi
}

# Convert any supported image to a flat PNG (no alpha, white background).
to_png() {
    local src="$1" dst="$2"
    local ext="${src##*.}"
    ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
    case "$ext" in
        pdf|eps|ps)
            _im -density 150 "${src}[0]" -background white -alpha remove -flatten "$dst" 2>/dev/null
            ;;
        *)
            _im -background white "$src" -alpha remove -flatten "$dst" 2>/dev/null
            ;;
    esac
}

# Prepend a coloured label strip to a PNG.
label_png() {
    local src="$1" dst="$2" label="$3" color="$4"
    local w
    w=$(_im_identify -format '%w' "$src" 2>/dev/null) || return 1
    _im \
        \( -size "${w}x32" xc:"$color" \
           -fill white -gravity Center -pointsize 22 -annotate 0 "$label" \) \
        \( "$src" \) \
        -append "$dst" 2>/dev/null
}

# Produce a side-by-side OLD/NEW collage PNG.
make_collage() {
    local old_png="$1" new_png="$2" out="$3"
    mkdir -p "$(dirname "$out")"

    label_png "$old_png" "$tmp/figcmp/old_lab.png" "OLD" "#CC2200" || return 1
    label_png "$new_png" "$tmp/figcmp/new_lab.png" "NEW" "#006622" || return 1

    local h_old h_new h_max
    h_old=$(_im_identify -format '%h' "$tmp/figcmp/old_lab.png" 2>/dev/null)
    h_new=$(_im_identify -format '%h' "$tmp/figcmp/new_lab.png" 2>/dev/null)
    [[ -n "$h_old" && -n "$h_new" ]] || return 1
    h_max=$(( h_old > h_new ? h_old : h_new ))

    _im "$tmp/figcmp/old_lab.png" -resize "x${h_max}" "$tmp/figcmp/old_siz.png" 2>/dev/null
    _im "$tmp/figcmp/new_lab.png" -resize "x${h_max}" "$tmp/figcmp/new_siz.png" 2>/dev/null
    _im "$tmp/figcmp/old_siz.png" "$tmp/figcmp/new_siz.png" \
        +append -bordercolor "#555555" -border 2 "$out" 2>/dev/null
}

# Parse \includegraphics references from both flat tex files and emit one TSV
# line per changed figure pair: old_abs_path \t new_abs_path \t display_name.
# Figures are matched by \label first, then by document order for unlabelled ones.
# Requires python3; returns exit code 1 if unavailable.
_match_figures_py() {
    local old_dir="$1" new_dir="$2"
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$old_dir" "$new_dir" "$tmp/old_flat.tex" "$tmp/new_flat.tex" <<'PYEOF'
import re, sys, os, hashlib

old_root, new_root, old_flat, new_flat = sys.argv[1:]

def parse_refs(tex_path):
    try:
        text = open(tex_path, encoding='utf-8', errors='replace').read()
    except Exception:
        return []

    ig_re        = re.compile(r'\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}')
    label_re     = re.compile(r'\\label\s*\{([^}]+)\}')
    fig_begin_re = re.compile(r'\\begin\{(?:figure\*?|wrapfigure\*?|subfigure\*?)\}')
    fig_end_re   = re.compile(r'\\end\{(?:figure\*?|wrapfigure\*?|subfigure\*?)\}')

    begins = [m.start() for m in fig_begin_re.finditer(text)]
    ends   = [m.start() for m in fig_end_re.finditer(text)]

    refs = []
    for igm in ig_re.finditer(text):
        pos = igm.start()
        env_s = max((s for s in begins if s < pos), default=None)
        env_e = min((e for e in ends   if e > pos), default=None)
        label = ''
        if env_s is not None and env_e is not None:
            lbls = label_re.findall(text[env_s:env_e])
            label = lbls[0] if lbls else ''
        refs.append({'label': label, 'path': igm.group(1).strip(), 'pos': pos})
    return refs

def resolve(path, root):
    for ext in ('', '.png', '.jpg', '.jpeg', '.pdf', '.eps', '.ps', '.svg', '.tiff', '.bmp'):
        p = os.path.join(root, path + ext)
        if os.path.isfile(p):
            return p
    return None

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

old_refs = parse_refs(old_flat)
new_refs = parse_refs(new_flat)

# Build label → ref map for old (first occurrence wins).
old_by_label = {}
for r in old_refs:
    if r['label'] and r['label'] not in old_by_label:
        old_by_label[r['label']] = r

# Match new refs to old refs: by label first, then by document order.
matched      = []
old_used     = set()
unmatched_new = []

for nr in new_refs:
    if nr['label'] and nr['label'] in old_by_label:
        or_ = old_by_label[nr['label']]
        matched.append((or_, nr))
        old_used.add(id(or_))
    else:
        unmatched_new.append(nr)

old_unmatched = [r for r in old_refs if id(r) not in old_used]
for j, nr in enumerate(unmatched_new):
    if j < len(old_unmatched):
        matched.append((old_unmatched[j], nr))

for or_, nr in matched:
    old_file = resolve(or_['path'], old_root)
    new_file = resolve(nr['path'], new_root)
    if old_file is None or new_file is None:
        continue
    if md5(old_file) == md5(new_file):
        continue
    name = nr['label'] if nr['label'] else nr['path']
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    print(f"{old_file}\t{new_file}\t{safe}")
PYEOF
}

# One collage step shared by both detection paths. Writes an index-prefixed
# collage PNG into $tmp/collages and appends "path<TAB>name" to the manifest.
_emit_collage() {
    local old_file="$1" new_file="$2" display_name="$3"
    local n; n=$(printf '%03d' "$collage_count")
    local old_png="$tmp/figcmp/src_old_${n}.png"
    local new_png="$tmp/figcmp/src_new_${n}.png"
    to_png "$old_file" "$old_png" \
        || { printf '  warning: cannot convert %s\n' "$old_file" >&2; return; }
    to_png "$new_file" "$new_png" \
        || { printf '  warning: cannot convert %s\n' "$new_file" >&2; return; }
    local collage="$tmp/collages/${n}_${display_name}.png"
    if make_collage "$old_png" "$new_png" "$collage"; then
        printf '%s\t%s\n' "$collage" "$display_name" >> "$tmp/fig_manifest.tsv"
        collage_count=$((collage_count + 1))
    else
        printf '  warning: collage failed for %s\n' "$display_name" >&2
    fi
}

# Find changed figures and produce OLD/NEW collages in $tmp/collages, recording
# each in $tmp/fig_manifest.tsv. Sets the global collage_count.
compare_figures() {
    local old_dir="$1" new_dir="$2"

    : > "$tmp/fig_manifest.tsv"
    collage_count=0

    if ! command -v convert >/dev/null 2>&1 && ! command -v magick >/dev/null 2>&1; then
        echo "warning: ImageMagick not found; skipping figure comparison" >&2
        return
    fi

    mkdir -p "$tmp/figcmp" "$tmp/collages"

    if _match_figures_py "$old_dir" "$new_dir" > "$tmp/fig_pairs.tsv" 2>/dev/null; then
        while IFS=$'\t' read -r old_file new_file display_name; do
            printf '  figure changed: %s\n' "$display_name"
            _emit_collage "$old_file" "$new_file" "$display_name"
        done < "$tmp/fig_pairs.tsv"
    else
        # Fallback: path-based comparison when python3 is unavailable.
        echo "warning: python3 not found; falling back to path-based figure comparison" >&2
        while IFS= read -r rel; do
            local old_file="$old_dir/$rel"
            [[ -f "$old_file" ]] || continue
            cmp -s "$old_file" "$new_dir/$rel" && continue
            printf '  figure changed: %s\n' "$rel"
            local safe="${rel//\//_}"; safe="${safe%.*}"
            _emit_collage "$old_file" "$new_dir/$rel" "$safe"
        done < <(
            cd "$new_dir" && find . -type f \
                \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' \
                   -o -iname '*.pdf' -o -iname '*.eps' -o -iname '*.ps'  \
                   -o -iname '*.svg' -o -iname '*.tiff' -o -iname '*.bmp' \) \
                | sed 's|^\./||' | sort
        )
    fi

    if [[ $collage_count -gt 0 ]]; then
        echo "figure diff: $collage_count figure(s) changed"
    else
        echo "figure diff: no figure changes detected"
    fi
}

# Build a standalone PDF (one collage per page) from the manifest, into $1.
build_fig_appendix() {
    local outpdf="$1"
    mkdir -p "$tmp/figapp"
    local texf="$tmp/figapp/figures.tex"
    {
        echo '\documentclass[a4paper]{article}'
        echo '\usepackage[margin=1.2cm]{geometry}'
        echo '\usepackage{graphicx}'
        echo '\pagestyle{empty}'
        echo '\setlength{\parindent}{0pt}'
        echo '\begin{document}'
        echo '\begin{center}{\Large\bfseries Figure changes (OLD vs.\ NEW)}\end{center}'
        echo '\vspace{1em}'
        local i=0
        while IFS=$'\t' read -r cpath cname; do
            # graphicx reads filenames verbatim, so give it underscore-free names.
            local img="$tmp/figapp/fig$(printf '%03d' "$i").png"
            cp "$cpath" "$img"
            # Escape LaTeX-special underscores in the human-readable caption.
            local esc="${cname//_/\\_}"
            echo "\\begin{center}"
            echo "{\\bfseries ${esc}}\\\\[0.5em]"
            echo "\\includegraphics[width=\\linewidth,height=0.85\\textheight,keepaspectratio]{${img}}"
            echo "\\end{center}"
            echo "\\clearpage"
            i=$((i + 1))
        done < "$tmp/fig_manifest.tsv"
        echo '\end{document}'
    } > "$texf"

    ( cd "$tmp/figapp" && pdflatex -interaction=nonstopmode figures.tex >/dev/null 2>&1 || true )
    [[ -f "$tmp/figapp/figures.pdf" ]] || return 1
    cp "$tmp/figapp/figures.pdf" "$outpdf"
}

# Concatenate PDFs $1 and $2 into $3 using whichever tool is available.
merge_pdfs() {
    local a="$1" b="$2" out="$3"
    if command -v pdfunite >/dev/null 2>&1; then
        pdfunite "$a" "$b" "$out" 2>/dev/null
    elif command -v gs >/dev/null 2>&1; then
        gs -q -dNOPAUSE -dBATCH -sDEVICE=pdfwrite -sOutputFile="$out" "$a" "$b" >/dev/null 2>&1
    else
        return 1
    fi
}

# ---- End figure comparison helpers ----------------------------------------

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [-m main.tex] [-M main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] OLD NEW

  OLD, NEW        The two project archives to compare. Each may be a .zip or a
                 tar archive (.tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz);
                 e.g. an Overleaf history export (.zip) or an arXiv source
                 download (.tar.gz). The two sides may use different formats.
  -m main.tex    Main .tex file in the OLD project (relative to its root).
                 Auto-detected (the file containing \\documentclass) if omitted.
  -M main.tex    Main .tex file in the NEW project. Auto-detected if omitted.
                 The two projects may use different main-file names.
  -o output.pdf  Output PDF path (default: <dir of NEW>/diff.pdf).
  -t TYPE        latexdiff --type value (default: UNDERLINE).
  -F             Do not append figure-diff collages to the PDF (on by default).
  -c DIR         Also save the figure-diff collage PNGs into DIR.
EOF
    exit 2
}

while getopts ":m:M:o:t:c:Fh" opt; do
    case "$opt" in
        m) main_old="$OPTARG" ;;
        M) main_new="$OPTARG" ;;
        o) out_pdf="$OPTARG" ;;
        t) diff_type="$OPTARG" ;;
        c) fig_dir="$OPTARG" ;;
        F) embed_figs=0 ;;
        h) usage ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))

[[ $# -eq 2 ]] || usage
old_arc="$1"
new_arc="$2"

for f in "$old_arc" "$new_arc"; do
    [[ -f "$f" ]] || { echo "error: not a file: $f" >&2; exit 1; }
done

# Which extractor an archive needs, by extension. Echoes the required command
# name (unzip/tar), or nothing for an unrecognised type. arXiv source downloads
# are tarballs; Overleaf history exports are zips.
archive_tool() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        *.zip)                                              echo unzip ;;
        *.tar|*.tar.gz|*.tgz|*.tar.bz2|*.tbz2|*.tar.xz|*.txz) echo tar ;;
        *)                                                  echo "" ;;
    esac
}

# Extract an archive into a destination directory, dispatching on its type.
extract_archive() {
    local arc="$1" dest="$2"
    case "$(archive_tool "$arc")" in
        unzip) unzip -q "$arc" -d "$dest" ;;
        # GNU tar and BSD tar (stock macOS) both auto-detect the compression
        # (gzip/bzip2/xz) on extraction, so a bare -xf handles every tar variant.
        tar)   tar -xf "$arc" -C "$dest" ;;
        *)     echo "error: unsupported archive type: $arc" >&2; exit 1 ;;
    esac
}

for cmd in latexdiff latexpand pdflatex; do
    command -v "$cmd" >/dev/null || { echo "error: missing dependency: $cmd" >&2; exit 1; }
done

# Validate each archive's type and that its extractor is installed (only the
# tools the given archives actually need are required).
for f in "$old_arc" "$new_arc"; do
    tool="$(archive_tool "$f")"
    if [[ -z "$tool" ]]; then
        echo "error: unsupported archive type: $f" >&2
        echo "       supported: .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz" >&2
        exit 1
    fi
    command -v "$tool" >/dev/null || { echo "error: missing dependency: $tool (needed for $f)" >&2; exit 1; }
done

# Resolve paths before we cd anywhere.
old_arc_abs="$(cd "$(dirname "$old_arc")" && pwd)/$(basename "$old_arc")"
new_arc_abs="$(cd "$(dirname "$new_arc")" && pwd)/$(basename "$new_arc")"
if [[ -z "$out_pdf" ]]; then
    out_pdf="$(dirname "$new_arc_abs")/diff.pdf"
fi
mkdir -p "$(dirname "$out_pdf")"
out_pdf="$(cd "$(dirname "$out_pdf")" && pwd)/$(basename "$out_pdf")"

tmp="$(mktemp -d -t latexdiff-zip.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/old" "$tmp/new" "$tmp/build"
extract_archive "$old_arc_abs" "$tmp/old"
extract_archive "$new_arc_abs" "$tmp/new"

# If the archive contained a single top-level directory, descend into it.
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

# Auto-detect the main .tex in a project root: the single file whose line starts
# with \documentclass. Echoes its basename, or exits with a helpful message when
# there are zero or several candidates. $2 is the flag to suggest on ambiguity.
detect_main() {
    local root="$1" flag="$2"
    local found=()
    # [[:space:]] rather than \s: BSD grep (stock macOS) has no \s. -h hides
    # filenames; we only need the names grep -l prints.
    while IFS= read -r f; do found+=("$f"); done \
        < <(grep -l -E '^[[:space:]]*\\documentclass' "$root"/*.tex 2>/dev/null || true)
    if [[ ${#found[@]} -eq 1 ]]; then
        basename "${found[0]}"
    elif [[ ${#found[@]} -gt 1 ]]; then
        echo "error: multiple candidate main .tex files in $root:" >&2
        printf '  %s\n' "${found[@]}" >&2
        echo "  pass $flag <filename> to choose one." >&2
        exit 1
    else
        echo "error: no .tex file with \\documentclass found in $root" >&2
        exit 1
    fi
}

# Resolve each project's main file independently (-m for old, -M for new); a
# side left unset is auto-detected. This lets the two zips use different
# main-file names, e.g. an Overleaf project that was renamed between exports.
[[ -n "$main_old" ]] || main_old="$(detect_main "$old_root" -m)"
[[ -n "$main_new" ]] || main_new="$(detect_main "$new_root" -M)"

[[ -f "$old_root/$main_old" ]] || { echo "error: $main_old not found in old project root ($old_root)" >&2; exit 1; }
[[ -f "$new_root/$main_new" ]] || { echo "error: $main_new not found in new project root ($new_root)" >&2; exit 1; }

if [[ "$main_old" == "$main_new" ]]; then
    echo "main file: $main_old"
else
    echo "main file: $main_old (old) -> $main_new (new)"
fi
echo "flattening..."
( cd "$old_root" && latexpand --keep-comments "$main_old" > "$tmp/old_flat.tex" 2>/dev/null )
( cd "$new_root" && latexpand --keep-comments "$main_new" > "$tmp/new_flat.tex" 2>/dev/null )

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
( cd "$new_root" && find . -type f ! -path "./$main_new" -print0 | \
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

echo "comparing figures..."
compare_figures "$old_root" "$new_root"

if [[ $collage_count -gt 0 ]]; then
    # Save the collages to a folder, if requested.
    if [[ -n "$fig_dir" ]]; then
        mkdir -p "$fig_dir"
        while IFS=$'\t' read -r cpath cname; do
            cp "$cpath" "$fig_dir/${cname}_diff.png"
        done < "$tmp/fig_manifest.tsv"
        echo "wrote figure collages to: $fig_dir/"
    fi

    # Append the collages to the diff PDF (default behaviour).
    if [[ $embed_figs -eq 1 ]]; then
        if build_fig_appendix "$tmp/figures.pdf" \
            && merge_pdfs "$out_pdf" "$tmp/figures.pdf" "$tmp/combined.pdf"; then
            cp "$tmp/combined.pdf" "$out_pdf"
            echo "appended $collage_count figure collage page(s) to the PDF"
        else
            echo "warning: could not embed figure collages (need pdfunite or gs to merge);" >&2
            echo "         re-run with -c DIR to save them as PNG files instead." >&2
        fi
    fi
fi

echo "wrote: $out_pdf"
