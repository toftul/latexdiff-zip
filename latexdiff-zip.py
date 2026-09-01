#!/usr/bin/env python3
"""latexdiff-zip: produce a latexdiff PDF between two LaTeX project archives.

Two snapshots downloaded from Overleaf history, or two arXiv source tarballs,
become one track-changes PDF -- text *and* figures. Each side may be a local
archive (.zip or a tar: .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz), and
the two sides may use different formats. Either side may instead be an arXiv
paper given as an id or URL (2401.12345v1, arXiv:hep-th/9901001,
https://arxiv.org/abs/...); its source is then fetched from arxiv.org.

Usage: latexdiff-zip [-m main.tex] [-M main.tex] [-o output.pdf] [-t TYPE]
                     [-F] [-c DIR] OLD NEW

The engine is stdlib-only: archives are unpacked with zipfile/tarfile and arXiv
sources fetched with urllib, so unzip/tar/curl are not required. It shells out
to the LaTeX toolchain (latexdiff, latexpand, pdflatex, bibtex/biber) and, for
the optional figure diff, to ImageMagick and pdfunite/gs -- all of which
degrade gracefully when absent. (This was originally a bash script; the
`v1-bash` git tag preserves it.) tests/test_parity.py is the executable spec.
"""

import filecmp
import getopt
import glob
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

UA = "latexdiff-zip (+https://github.com/toftul/latexdiff-zip)"
DEVNULL = subprocess.DEVNULL

# latexdiff's default graphics markup ("new-only") comments deleted
# \includegraphics commands away, so a figure the new version dropped just
# vanishes from the diff. "both" keeps them live -- drawn at reduced scale with
# a red cross -- which is why the build also needs the old files
# (copy_old_figures). Given up on the last retry: latexdiff's own manual warns
# it can provoke "Misplaced \noalign" on some tables.
GRAPHICS_MARKUP = "--graphics-markup=both"

# Image extensions tried when resolving an \includegraphics path with no suffix.
RESOLVE_EXTS = ("", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps", ".svg",
                ".tiff", ".bmp")

_DOCCLASS_RE = re.compile(r"^[^\S\n]*\\documentclass", re.M)
_BIBLATEX_RE = re.compile(
    r"^[^%]*\\(usepackage(\[[^]]*\])?\{[^}]*biblatex|addbibresource|printbibliography)",
    re.M)
_BIBTEX_RE = re.compile(r"^[^%]*\\bibliography[^\S\n]*\{", re.M)
_ARXIV_RE = re.compile(
    r"([0-9]{4}\.[0-9]{4,5}|[a-z-]+(\.[A-Z]{2})?/[0-9]{7})(v[0-9]+)?")


# ---- small output / fs helpers --------------------------------------------

def say(msg):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    warn(msg)
    sys.exit(code)


def which(cmd):
    return shutil.which(cmd) is not None


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def file_has(path, needle):
    return needle in read_text(path)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def tail_to_stderr(path, n):
    try:
        lines = read_text(path).splitlines()
    except Exception:
        return
    for line in lines[-n:]:
        warn(line)


# ---- CLI ------------------------------------------------------------------

def usage():
    prog = os.path.basename(sys.argv[0])
    warn(
        f"Usage: {prog} [-m main.tex] [-M main.tex] [-o output.pdf] [-t TYPE] [-F] [-c DIR] OLD NEW\n"
        "\n"
        "  OLD, NEW       The two versions to compare. Each is either a local project\n"
        "                 archive -- .zip or tar (.tar, .tar.gz/.tgz, .tar.bz2/.tbz2,\n"
        "                 .tar.xz/.txz), e.g. an Overleaf history export or an arXiv\n"
        "                 source download -- or an arXiv paper to fetch, given as an id\n"
        "                 (2401.12345v1, hep-th/9901001), an arXiv:<id> reference, or an\n"
        "                 arxiv.org URL (abs/pdf/e-print). The two sides may differ.\n"
        "  -m main.tex    Main .tex file in the OLD project (relative to its root).\n"
        "                 Auto-detected (the file containing \\documentclass) if omitted.\n"
        "  -M main.tex    Main .tex file in the NEW project. Auto-detected if omitted.\n"
        "                 The two projects may use different main-file names.\n"
        "  -o output.pdf  Output PDF path (default: <dir of NEW>/diff.pdf).\n"
        "  -t TYPE        latexdiff --type value (default: UNDERLINE).\n"
        "  -F             Do not append figure-diff collages to the PDF (on by default).\n"
        "  -c DIR         Also save the figure-diff collage PNGs into DIR."
    )
    sys.exit(2)


def parse_args(argv):
    main_old = ""
    main_new = ""
    out_pdf = ""
    diff_type = "UNDERLINE"
    embed_figs = True
    fig_dir = ""
    try:
        opts, rest = getopt.getopt(argv, "m:M:o:t:c:Fh")
    except getopt.GetoptError:
        usage()
    for opt, val in opts:
        if opt == "-m":
            main_old = val
        elif opt == "-M":
            main_new = val
        elif opt == "-o":
            out_pdf = val
        elif opt == "-t":
            diff_type = val
        elif opt == "-c":
            fig_dir = val
        elif opt == "-F":
            embed_figs = False
        elif opt == "-h":
            usage()
    if len(rest) != 2:
        usage()
    return {
        "main_old": main_old, "main_new": main_new, "out_pdf": out_pdf,
        "diff_type": diff_type, "embed_figs": embed_figs, "fig_dir": fig_dir,
        "old_arc": rest[0], "new_arc": rest[1],
    }


# ---- inputs: arXiv ids and archives ---------------------------------------

def normalize_arxiv(arg):
    """Return the bare arXiv id (e.g. 2401.12345v2 or hep-th/9901001) inside an
    argument that may be a raw id, an arXiv:<id> reference, or an arxiv.org URL
    (abs/pdf/e-print/src), or None if it doesn't look like an arXiv id."""
    s = arg.split("?", 1)[0]                      # drop a pasted URL's ?query
    for pre in ("http://", "https://"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    for pre in ("www.", "export."):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.startswith("arxiv.org/"):
        s = s[len("arxiv.org/"):]
        i = s.find("/")                           # drop abs/pdf/e-print/src
        if i >= 0:
            s = s[i + 1:]
    if s[:6].lower() == "arxiv:":
        s = s[s.find(":") + 1:]
    if s.endswith(".pdf"):
        s = s[:-4]
    return s if _ARXIV_RE.fullmatch(s) else None


def archive_tool(name):
    """'zip', 'tar', or '' by filename extension."""
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                   ".tar.xz", ".txz")):
        return "tar"
    return ""


def _safe_extract_tar(tf, dest):
    # filter='data' (Python 3.12+) blocks path traversal / unsafe members;
    # fall back to a plain extract on older interpreters.
    try:
        tf.extractall(dest, filter="data")
    except TypeError:
        tf.extractall(dest)


def extract_archive(arc, dest):
    tool = archive_tool(arc)
    if tool == "zip":
        with zipfile.ZipFile(arc) as zf:
            zf.extractall(dest)
    elif tool == "tar":
        with tarfile.open(arc, "r:*") as tf:      # auto-detects gzip/bzip2/xz
            _safe_extract_tar(tf, dest)
    else:
        die(f"error: unsupported archive type: {arc}")


def http_get(url, out):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(out, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except Exception:
        return False


def _tar_nonempty(path):
    """True if `path` is a tar (any compression) with at least one entry. The
    non-empty check matters: a gzipped file whose decompressed content is
    shorter than one 512-byte block can read as a valid *empty* tar, which
    would swallow a tiny single-file arXiv submission."""
    try:
        with tarfile.open(path, "r:*") as tf:
            return tf.next() is not None
    except Exception:
        return False


def fetch_arxiv(arxiv_id, dest, tmp):
    """Fetch an arXiv e-print and unpack it into `dest`. The e-print endpoint
    serves a (gzipped) tar for multi-file submissions, a bare gzipped .tex for
    single-file ones, or a PDF when there is no TeX source."""
    dl = os.path.join(tmp, os.path.basename(dest) + ".eprint")
    say(f"fetching arXiv:{arxiv_id} source...")
    if not http_get(f"https://arxiv.org/e-print/{arxiv_id}", dl):
        warn(f"error: could not download https://arxiv.org/e-print/{arxiv_id}")
        die("       check the id (and version), and your network connection")
    with open(dl, "rb") as f:
        magic = f.read(4)
    if magic[:4] == b"%PDF":                      # pdf-only submission
        die(f"error: arXiv:{arxiv_id} has no LaTeX source (pdf-only submission)")
    elif _tar_nonempty(dl):
        with tarfile.open(dl, "r:*") as tf:
            _safe_extract_tar(tf, dest)
    elif magic[:2] == b"\x1f\x8b":                # gzip but not a tar: single .tex
        with gzip.open(dl) as gz:
            data = gz.read()
        with open(os.path.join(dest, "main.tex"), "wb") as f:
            f.write(data)
    else:
        die(f"error: unrecognised e-print format for arXiv:{arxiv_id}")


def descend_single_root(d):
    """If the archive contained a single top-level directory, descend into it."""
    entries = [os.path.join(d, e) for e in os.listdir(d)]
    if len(entries) == 1 and os.path.isdir(entries[0]):
        return entries[0]
    return d


# ---- project: main file ---------------------------------------------------

def detect_main(root, flag, other=""):
    """The single *.tex in `root` whose line starts with \\documentclass.
    `other` (the other side's main basename) is dropped from the candidates on
    ambiguity, so one archive holding both an old and a new main can be diffed
    by naming just one side. Exits with a helpful message on 0 or >1 matches."""
    found = sorted(p for p in glob.glob(os.path.join(root, "*.tex"))
                   if _DOCCLASS_RE.search(read_text(p)))
    if len(found) > 1 and other:
        ob = os.path.basename(other)
        found = [f for f in found if os.path.basename(f) != ob]
    if len(found) == 1:
        return os.path.basename(found[0])
    if len(found) > 1:
        warn(f"error: multiple candidate main .tex files in {root}:")
        for f in found:
            warn(f"  {f}")
        die(f"  pass {flag} <filename> to choose one.")
    die(f"error: no .tex file with \\documentclass found in {root}")


# ---- flattening & bibliography --------------------------------------------

def latexpand(root, main, out_path, *extra):
    with open(out_path, "wb") as out:
        subprocess.run(["latexpand", "--keep-comments", *extra, main],
                       cwd=root, stdout=out, stderr=DEVNULL)


def bib_kind(flat_path):
    """Which bibliography system the flattened document uses."""
    text = read_text(flat_path)
    if _BIBLATEX_RE.search(text):
        return "biblatex"
    if _BIBTEX_RE.search(text):
        return "bibtex"
    return "none"


def make_bbl(root, main):
    """Produce a formatted .bbl for a project side; return its name relative to
    the project root, or None when no usable one (>= 1 \\bibitem) came out.

    When the project has .bib database(s) the .bbl is regenerated (draft
    compile -- only the .aux matters -- then bibtex). But arXiv sources ship
    the formatted .bbl *instead of* the .bib, and bibtex, finding no database,
    would overwrite that good .bbl with an empty one -- so a shipped .bbl is
    stashed first and restored if regeneration comes out unusable."""
    job = os.path.splitext(os.path.basename(main))[0]
    bbl = os.path.join(root, job + ".bbl")
    stash = os.path.join(root, ".ldz_shipped.bbl")
    remove(stash)
    if os.path.isfile(bbl) and os.path.getsize(bbl) > 0:
        shutil.copy(bbl, stash)
    has_bib = any(fn.endswith(".bib")
                  for _dp, _dn, files in os.walk(root) for fn in files)
    if which("bibtex") and has_bib:
        subprocess.run(["pdflatex", "-interaction=nonstopmode", "-draftmode", main],
                       cwd=root, stdout=DEVNULL, stderr=DEVNULL)
        subprocess.run(["bibtex", job], cwd=root, stdout=DEVNULL, stderr=DEVNULL)
    if not file_has(bbl, "\\bibitem") and os.path.isfile(stash):
        shutil.copy(stash, bbl)
    remove(stash)
    if not file_has(bbl, "\\bibitem"):
        return None
    return job + ".bbl"


def _read_group(text, p):
    """Read one brace-balanced {...} group at text[p:], skipping leading
    whitespace. Returns (content, index-after-group) or (None, p)."""
    n = len(text)
    while p < n and text[p] in " \t\n":
        p += 1
    if p >= n or text[p] != "{":
        return None, p
    depth, start = 0, p
    while p < n:
        if text[p] == "{":
            depth += 1
        elif text[p] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:p], p + 1
        p += 1
    return None, start


_HREF_RE = re.compile(r"\\href(?:@noop)?(?![a-zA-Z@])")
_BEGIN_BIB_RE = re.compile(r"\\begin\{thebibliography\}")
_END_BIB_RE = re.compile(r"\\end\{thebibliography\}")
# \s* between the parts: REVTeX-family styles emit "\bibitem [{...}]{key}".
_BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")


def _strip_links(text):
    """Unwrap \\href{url}{text} (and REVTeX's \\href@noop) to plain text. ulem's
    \\uwave -- the UNDERLINE markup -- loops or overflows TeX's input stack on
    hyperlinks inside changed text, so a diff PDF trades reference links for a
    bibliography that compiles."""
    out, i = [], 0
    for m in _HREF_RE.finditer(text):
        if m.start() < i:
            continue                              # inside a group already consumed
        url, p1 = _read_group(text, m.end())
        if url is None:                           # e.g. the \providecommand defs
            continue
        label, p2 = _read_group(text, p1)
        if label is None:
            continue
        out.append(text[i:m.start()])
        out.append(label)
        i = p2
    out.append(text[i:])
    return "".join(out)


def _parse_bbl(text):
    """Split a thebibliography body into (head, [(key, block), ...], tail), or
    None if it isn't a parseable bibliography."""
    end = _END_BIB_RE.search(text)
    if not _BEGIN_BIB_RE.search(text) or not end:
        return None
    items = list(_BIBITEM_RE.finditer(text))
    if not items or items[0].start() > end.start():
        return None
    head = text[:items[0].start()]
    entries = []
    for i, m in enumerate(items):
        stop = items[i + 1].start() if i + 1 < len(items) else end.start()
        entries.append((m.group(1), text[m.start():stop]))
    return head, entries, text[end.start():]


def prepare_bbls(old_bbl_path, new_bbl_path):
    """In-place, best-effort. Strip hyperlinks from each .bbl, and line the OLD
    entries up with the NEW .bbl's key order. latexdiff pairs bibliography
    entries essentially by position -- entries share so much boilerplate that
    two shifted skeletons outscore one entry's real content across a shift, so
    one added reference would mark every later entry changed. Each old entry
    therefore goes to the slot its key occupies in the new .bbl; old-only
    entries fill the slots of new-only keys (one visibly replaced entry) and
    any surplus goes to the end, where it shows as plainly deleted."""
    try:
        texts = {}
        for path in (old_bbl_path, new_bbl_path):
            if path:
                texts[path] = _strip_links(read_text(path))

        if old_bbl_path and new_bbl_path:
            parsed_old = _parse_bbl(texts[old_bbl_path])
            parsed_new = _parse_bbl(texts[new_bbl_path])
            if parsed_old and parsed_new:
                old_head, old_entries, old_tail = parsed_old
                _, new_entries, _ = parsed_new

                old_by_key = {}
                for k, block in old_entries:
                    old_by_key.setdefault(k, block)

                slots = [None] * len(new_entries)
                used = set()
                for i, (k, _b) in enumerate(new_entries):
                    if k in old_by_key and k not in used:
                        slots[i] = old_by_key[k]
                        used.add(k)

                leftover = []
                for k, block in old_entries:
                    if k not in used:
                        leftover.append(block)
                        used.add(k)
                li = iter(leftover)
                out = []
                for s in slots:
                    if s is None:
                        s = next(li, None)
                    if s is not None:
                        out.append(s)
                out.extend(li)                    # surplus removals go to the end

                texts[old_bbl_path] = old_head + "".join(out) + old_tail

        for path, text in texts.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    except Exception:
        pass                                      # never let bib prep break a run


# ---- figure comparison ----------------------------------------------------

_IG_RE = re.compile(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}")
_LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
_FIGBEG_RE = re.compile(r"\\begin\{(?:figure\*?|wrapfigure\*?|subfigure\*?)\}")
_FIGEND_RE = re.compile(r"\\end\{(?:figure\*?|wrapfigure\*?|subfigure\*?)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*")


def _im_cmd():
    return ["magick"] if which("magick") else ["convert"]


def _im_identify_cmd():
    return ["magick", "identify"] if which("magick") else ["identify"]


def _im(args):
    return subprocess.run(_im_cmd() + args, stdout=DEVNULL, stderr=DEVNULL).returncode == 0


def _im_identify(args):
    p = subprocess.run(_im_identify_cmd() + args, stdout=subprocess.PIPE,
                       stderr=DEVNULL, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def to_png(src, dst):
    """Convert any supported image to a flat PNG (no alpha, white background)."""
    ext = os.path.splitext(src)[1].lstrip(".").lower()
    if ext in ("pdf", "eps", "ps"):
        return _im(["-density", "150", f"{src}[0]", "-background", "white",
                    "-alpha", "remove", "-flatten", dst])
    return _im(["-background", "white", src, "-alpha", "remove", "-flatten", dst])


def label_png(src, dst, label, color):
    """Prepend a coloured label strip to a PNG."""
    w = _im_identify(["-format", "%w", src])
    if not w:
        return False
    return _im(["(", "-size", f"{w}x32", f"xc:{color}", "-fill", "white",
                "-gravity", "Center", "-pointsize", "22", "-annotate", "0", label, ")",
                "(", src, ")", "-append", dst])


def make_collage(old_png, new_png, out, figcmp):
    """Produce a side-by-side OLD/NEW collage PNG."""
    old_lab = os.path.join(figcmp, "old_lab.png")
    new_lab = os.path.join(figcmp, "new_lab.png")
    if not label_png(old_png, old_lab, "OLD", "#CC2200"):
        return False
    if not label_png(new_png, new_lab, "NEW", "#006622"):
        return False
    h_old = _im_identify(["-format", "%h", old_lab])
    h_new = _im_identify(["-format", "%h", new_lab])
    if not h_old or not h_new:
        return False
    h_max = max(int(h_old), int(h_new))
    old_siz = os.path.join(figcmp, "old_siz.png")
    new_siz = os.path.join(figcmp, "new_siz.png")
    _im([old_lab, "-resize", f"x{h_max}", old_siz])
    _im([new_lab, "-resize", f"x{h_max}", new_siz])
    return _im([old_siz, new_siz, "+append", "-bordercolor", "#555555",
                "-border", "2", out])


def make_single(src_png, out, label, color, figcmp):
    """A single labelled panel, for a figure that exists on only one side."""
    lab = os.path.join(figcmp, "single_lab.png")
    if not label_png(src_png, lab, label, color):
        return False
    return _im([lab, "-bordercolor", "#555555", "-border", "2", out])


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Drop an edited pair whose rasterised images differ in fewer than this fraction
# of pixels: the change is only in the file bytes (e.g. a new PDF /CreationDate),
# not on the page.
VISUAL_SKIP_FRACTION = 0.0005


def _visually_identical(a, b):
    """True if rasterised figures a and b look the same. Catches a regenerated
    file whose bytes differ but whose pixels do not (matplotlib/Inkscape stamp a
    fresh timestamp on every export). Any comparison failure -- including a size
    change, which is a genuine difference -- returns False, so the pair is kept."""
    cmp_cmd = ["magick", "compare"] if which("magick") else ["compare"]
    p = subprocess.run(cmp_cmd + ["-metric", "AE", "-fuzz", "1%", a, b, "null:"],
                       stdout=DEVNULL, stderr=subprocess.PIPE, text=True)
    if p.returncode >= 2:                          # incomparable (e.g. size change)
        return False
    try:
        ae = float(p.stderr.strip().split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return False
    if ae == 0:
        return True
    dims = _im_identify(["-format", "%w %h", a])
    if dims:
        try:
            w, h = (int(x) for x in dims.split()[:2])
            if w * h and ae / (w * h) < VISUAL_SKIP_FRACTION:
                return True
        except (ValueError, ZeroDivisionError):
            pass
    return False


def _parse_graphicspath(text):
    """Directories declared by \\graphicspath{{dir1/}{dir2/}}, in order."""
    dirs = []
    for gm in _GRAPHICSPATH_RE.finditer(text):
        inner, _ = _read_group(text, gm.end())
        if inner is None:
            continue
        for dm in re.finditer(r"\{([^{}]*)\}", inner):
            d = dm.group(1).strip()
            if d and d not in dirs:
                dirs.append(d)
    return dirs


def _resolve(path, root, graphics_paths=()):
    """Locate a figure file the way LaTeX would: search the document directory
    plus each \\graphicspath directory, trying known extensions when the
    reference has none. The document directory is tried first, so an explicit
    'figs/plot' still resolves even when \\graphicspath{{figs/}} is also set."""
    for prefix in ("", *graphics_paths):
        for ext in RESOLVE_EXTS:
            p = os.path.join(root, prefix, path + ext)
            if os.path.isfile(p):
                return p
    return None


def _parse_refs(text):
    begins = [m.start() for m in _FIGBEG_RE.finditer(text)]
    ends = [m.start() for m in _FIGEND_RE.finditer(text)]
    refs = []
    for igm in _IG_RE.finditer(text):
        pos = igm.start()
        env_s = max((s for s in begins if s < pos), default=None)
        env_e = min((e for e in ends if e > pos), default=None)
        label = ""
        if env_s is not None and env_e is not None:
            lbls = _LABEL_RE.findall(text[env_s:env_e])
            label = lbls[0] if lbls else ""
        refs.append({"label": label, "path": igm.group(1).strip(), "pos": pos})
    return refs


def _fig_name(ref):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", ref["label"] or ref["path"])


def match_figures(old_root, new_root, old_flat, new_flat):
    """Pair figures between the two versions by \\label first, then by document
    order. Returns three lists of resolved files:

        changed = [(old_file, new_file, name)]  paired but byte-different
        added   = [(new_file, name)]            only in the new version
        removed = [(old_file, name)]            only in the old version

    Byte-identical pairs are dropped here; pairs that differ only in bytes but
    not in pixels are dropped later, once rasterised."""
    old_text = read_text(old_flat)
    new_text = read_text(new_flat)
    old_refs = _parse_refs(old_text)
    new_refs = _parse_refs(new_text)
    old_gp = _parse_graphicspath(old_text)
    new_gp = _parse_graphicspath(new_text)

    # Label -> that label's old refs, in document order. A multi-panel figure
    # written as several \includegraphics under one \label contributes one
    # entry per panel, and each old panel is consumed by at most one new one:
    # pairing every new panel with the same old ref would compare unrelated
    # images and then report the remaining old panels as removed.
    old_by_label = {}
    for r in old_refs:
        if r["label"]:
            old_by_label.setdefault(r["label"], []).append(r)

    matched, old_used, unmatched_new = [], set(), []
    for nr in new_refs:
        pool = old_by_label.get(nr["label"]) if nr["label"] else None
        if pool:
            or_ = pool.pop(0)
            matched.append((or_, nr))
            old_used.add(id(or_))
        else:
            unmatched_new.append(nr)

    old_unmatched = [r for r in old_refs if id(r) not in old_used]
    k = min(len(unmatched_new), len(old_unmatched))
    for j in range(k):
        matched.append((old_unmatched[j], unmatched_new[j]))
    added_refs = unmatched_new[k:]
    removed_refs = old_unmatched[k:]

    changed = []
    for or_, nr in matched:
        old_file = _resolve(or_["path"], old_root, old_gp)
        new_file = _resolve(nr["path"], new_root, new_gp)
        if old_file is None or new_file is None:
            continue
        if _md5(old_file) == _md5(new_file):
            continue
        changed.append((old_file, new_file, _fig_name(nr)))

    added = [(f, _fig_name(nr)) for nr in added_refs
             if (f := _resolve(nr["path"], new_root, new_gp)) is not None]
    removed = [(f, _fig_name(or_)) for or_ in removed_refs
               if (f := _resolve(or_["path"], old_root, old_gp)) is not None]
    return changed, added, removed


def copy_old_figures(old_root, old_flat, new_flat, build):
    """Put the OLD version's figures within reach of the compile.

    The build dir holds the new project's assets, so an \\includegraphics that
    only the old version had -- a figure dropped or renamed since -- would come
    up missing. latexdiff keeps those commands live (see GRAPHICS_MARKUP) to
    draw the deleted figure crossed out, so the file has to be there.

    Each old reference the build cannot already satisfy is copied in under the
    name the deleted command asks for. A reference that *does* resolve is left
    alone, so the new project's asset always wins over an old namesake.
    Returns how many were copied."""
    old_text = read_text(old_flat)
    old_gp = _parse_graphicspath(old_text)
    new_gp = _parse_graphicspath(read_text(new_flat))
    n = 0
    for ref in _parse_refs(old_text):
        if _resolve(ref["path"], build, new_gp) is not None:
            continue                              # the new project provides it
        src = _resolve(ref["path"], old_root, old_gp)
        if src is None:
            continue
        # File it under the reference as written (plus the resolved suffix when
        # the reference has none): LaTeX looks in the document directory
        # whatever \graphicspath says, so this resolves either way.
        rel = ref["path"]
        if not os.path.splitext(rel)[1]:
            rel += os.path.splitext(src)[1]
        dst = os.path.join(build, rel)
        if os.path.exists(dst):
            continue
        try:
            os.makedirs(os.path.dirname(dst) or build, exist_ok=True)
            shutil.copy(src, dst)
        except OSError:
            continue
        n += 1
    return n


def _emit_single(refs, verb, label, color, figcmp, collages, manifest):
    """One single-panel page per one-sided figure. Returns how many were added."""
    count = 0
    for i, (src, name) in enumerate(refs):
        png = os.path.join(figcmp, f"src_{verb}_{i:03d}.png")
        if not to_png(src, png):
            warn(f"  warning: cannot convert {src}")
            continue
        say(f"  figure {verb}: {name}")
        collage = os.path.join(collages, f"{verb}_{i:03d}_{name}.png")
        if make_single(png, collage, label, color, figcmp):
            manifest.append((collage, name))
            count += 1
        else:
            warn(f"  warning: collage failed for {name}")
    return count


def compare_figures(old_root, new_root, old_flat, new_flat, tmp):
    """Return a manifest [(collage_png, display_name), ...] describing every
    figure change: edited pairs (side-by-side OLD/NEW), plus figures added or
    removed (a single labelled panel). Edited pairs whose rasterised pixels are
    identical -- only the file bytes changed -- are dropped. Degrades to an empty
    manifest (with a warning) when ImageMagick is absent."""
    if not (which("magick") or which("convert")):
        warn("warning: ImageMagick not found; skipping figure comparison")
        return []
    figcmp = os.path.join(tmp, "figcmp")
    collages = os.path.join(tmp, "collages")
    os.makedirs(figcmp, exist_ok=True)
    os.makedirs(collages, exist_ok=True)

    changed, added, removed = match_figures(old_root, new_root, old_flat, new_flat)

    manifest = []
    n_changed = 0
    for i, (old_file, new_file, name) in enumerate(changed):
        n = f"{i:03d}"
        old_png = os.path.join(figcmp, f"src_old_{n}.png")
        new_png = os.path.join(figcmp, f"src_new_{n}.png")
        if not to_png(old_file, old_png):
            warn(f"  warning: cannot convert {old_file}")
            continue
        if not to_png(new_file, new_png):
            warn(f"  warning: cannot convert {new_file}")
            continue
        if _visually_identical(old_png, new_png):
            continue                               # same pixels, only bytes changed
        say(f"  figure changed: {name}")
        collage = os.path.join(collages, f"chg_{n}_{name}.png")
        if make_collage(old_png, new_png, collage, figcmp):
            manifest.append((collage, name))
            n_changed += 1
        else:
            warn(f"  warning: collage failed for {name}")

    n_added = _emit_single(added, "added", "NEW ONLY", "#006622",
                           figcmp, collages, manifest)
    n_removed = _emit_single(removed, "removed", "OLD ONLY", "#CC2200",
                             figcmp, collages, manifest)

    parts = []
    if n_changed:
        parts.append(f"{n_changed} changed")
    if n_added:
        parts.append(f"{n_added} added")
    if n_removed:
        parts.append(f"{n_removed} removed")
    if parts:
        say("figure diff: " + ", ".join(parts) + " figure(s)")
    else:
        say("figure diff: no figure changes detected")
    return manifest


def build_fig_appendix(manifest, outpdf, tmp):
    """Build a standalone PDF (one collage per page) from the manifest."""
    figapp = os.path.join(tmp, "figapp")
    os.makedirs(figapp, exist_ok=True)
    lines = [
        r"\documentclass[a4paper]{article}",
        r"\usepackage[margin=1.2cm]{geometry}",
        r"\usepackage{graphicx}",
        r"\pagestyle{empty}",
        r"\setlength{\parindent}{0pt}",
        r"\begin{document}",
        r"\begin{center}{\Large\bfseries Figure changes (OLD vs.\ NEW)}\end{center}",
        r"\vspace{1em}",
    ]
    for i, (cpath, cname) in enumerate(manifest):
        # graphicx reads filenames verbatim, so give it an underscore-free name.
        img = os.path.join(figapp, f"fig{i:03d}.png")
        shutil.copy(cpath, img)
        esc = cname.replace("_", r"\_")           # escape for the caption
        lines += [
            r"\begin{center}",
            r"{\bfseries " + esc + r"}\\[0.5em]",
            r"\includegraphics[width=\linewidth,height=0.85\textheight,keepaspectratio]{" + img + "}",
            r"\end{center}",
            r"\clearpage",
        ]
    lines.append(r"\end{document}")
    with open(os.path.join(figapp, "figures.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")

    subprocess.run(["pdflatex", "-interaction=nonstopmode", "figures.tex"],
                   cwd=figapp, stdout=DEVNULL, stderr=DEVNULL)
    figpdf = os.path.join(figapp, "figures.pdf")
    if not os.path.isfile(figpdf):
        return False
    shutil.copy(figpdf, outpdf)
    return True


def merge_pdfs(a, b, out):
    """Concatenate PDFs a and b into out with whichever tool is available."""
    if which("pdfunite"):
        return subprocess.run(["pdfunite", a, b, out],
                              stdout=DEVNULL, stderr=DEVNULL).returncode == 0
    if which("gs"):
        return subprocess.run(
            ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
             f"-sOutputFile={out}", a, b], stdout=DEVNULL, stderr=DEVNULL
        ).returncode == 0
    return False


# ---- PDF build ------------------------------------------------------------

def runtex(build):
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "diff.tex"],
                   cwd=build, stdout=DEVNULL, stderr=DEVNULL)


def build_diff_pdf(build, main_new):
    """The fixed pdflatex -> bib backend -> pdflatex x2 sequence, ignoring
    non-zero exits (latexdiff markup routinely trips minor errors; the classic
    sequence still converges where latexmk stops too early). Returns whether a
    diff.pdf came out."""
    for f in ("diff.aux", "diff.bbl", "diff.bcf", "diff.blg", "diff.pdf"):
        remove(os.path.join(build, f))

    runtex(build)                                 # pass 1: write .aux

    diff_aux = os.path.join(build, "diff.aux")
    diff_bcf = os.path.join(build, "diff.bcf")
    diff_bbl = os.path.join(build, "diff.bbl")
    if os.path.isfile(diff_bcf):
        if which("biber"):
            subprocess.run(["biber", "diff"], cwd=build, stdout=DEVNULL, stderr=DEVNULL)
        else:
            warn("warning: document needs biber but it is not installed; citations may stay unresolved")
    elif file_has(diff_aux, "\\bibdata"):
        if which("bibtex"):
            subprocess.run(["bibtex", "diff"], cwd=build, stdout=DEVNULL, stderr=DEVNULL)
        else:
            warn("warning: document needs bibtex but it is not installed; citations may stay unresolved")

    # If a backend was needed but produced no usable diff.bbl (arXiv sources
    # ship the formatted .bbl instead of the .bib, and the diff compiles under
    # jobname "diff", so the shipped <main>.bbl is never read), adopt the new
    # project's shipped .bbl -- it was copied in with the assets.
    needs_bib = os.path.isfile(diff_bcf) or file_has(diff_aux, "\\bibdata")
    if needs_bib and not (file_has(diff_bbl, "\\bibitem") or file_has(diff_bbl, "\\entry")):
        shipped = os.path.join(build, os.path.splitext(os.path.basename(main_new))[0] + ".bbl")
        if os.path.isfile(shipped) and os.path.getsize(shipped) > 0:
            shutil.copy(shipped, diff_bbl)

    runtex(build)                                 # pass 2: pull in .bbl, resolve labels
    runtex(build)                                 # pass 3: read \bibcite back
    return os.path.isfile(os.path.join(build, "diff.pdf"))


# ---- main -----------------------------------------------------------------

def main(argv):
    args = parse_args(argv)
    main_old = args["main_old"]
    main_new = args["main_new"]
    out_pdf = args["out_pdf"]
    diff_type = args["diff_type"]
    embed_figs = args["embed_figs"]
    fig_dir = args["fig_dir"]
    old_arc = args["old_arc"]
    new_arc = args["new_arc"]

    # Each side is either a local archive file or an arXiv paper to fetch; a
    # local file wins, so a file named like an id still works.
    old_id = new_id = None
    if not os.path.isfile(old_arc):
        old_id = normalize_arxiv(old_arc)
        if old_id is None:
            die(f"error: not a file or arXiv id: {old_arc}")
    if not os.path.isfile(new_arc):
        new_id = normalize_arxiv(new_arc)
        if new_id is None:
            die(f"error: not a file or arXiv id: {new_arc}")

    for cmd in ("latexdiff", "latexpand", "pdflatex"):
        if not which(cmd):
            die(f"error: missing dependency: {cmd}")

    # Validate each local archive's type (arXiv sides aren't local files).
    for f in (old_arc, new_arc):
        if not os.path.isfile(f):
            continue
        if not archive_tool(f):
            warn(f"error: unsupported archive type: {f}")
            die("       supported: .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz")

    old_arc_abs = None if old_id else os.path.abspath(old_arc)
    new_arc_abs = None if new_id else os.path.abspath(new_arc)

    if not out_pdf:
        if new_id:
            out_pdf = os.path.join(os.getcwd(), "diff.pdf")   # NEW fetched: no archive to sit beside
        else:
            out_pdf = os.path.join(os.path.dirname(new_arc_abs), "diff.pdf")
    out_dir = os.path.dirname(out_pdf) or "."
    os.makedirs(out_dir, exist_ok=True)
    out_pdf = os.path.join(os.path.abspath(out_dir), os.path.basename(out_pdf))
    if fig_dir:
        fig_dir = os.path.abspath(fig_dir)

    tmp = tempfile.mkdtemp(prefix="latexdiff-zip.")
    try:
        old_dir = os.path.join(tmp, "old")
        new_dir = os.path.join(tmp, "new")
        build = os.path.join(tmp, "build")
        for d in (old_dir, new_dir, build):
            os.makedirs(d)

        if old_id:
            fetch_arxiv(old_id, old_dir, tmp)
        else:
            extract_archive(old_arc_abs, old_dir)
        if new_id:
            fetch_arxiv(new_id, new_dir, tmp)
        else:
            extract_archive(new_arc_abs, new_dir)

        old_root = descend_single_root(old_dir)
        new_root = descend_single_root(new_dir)

        # Same-input special case: the two sides are the *same* archive/id, so a
        # project holding both an old and a new main can be diffed by naming
        # just one side (auto-detection of the other excludes the named main).
        if old_id or new_id:
            same_input = old_id == new_id
        else:
            same_input = (old_arc_abs == new_arc_abs
                          or filecmp.cmp(old_arc_abs, new_arc_abs, shallow=False))
        excl_old = main_new if same_input else ""
        excl_new = main_old if same_input else ""
        if not main_old:
            main_old = detect_main(old_root, "-m", excl_old)
        if not main_new:
            main_new = detect_main(new_root, "-M", excl_new)

        if not os.path.isfile(os.path.join(old_root, main_old)):
            die(f"error: {main_old} not found in old project root ({old_root})")
        if not os.path.isfile(os.path.join(new_root, main_new)):
            die(f"error: {main_new} not found in new project root ({new_root})")

        if main_old == main_new:
            say(f"main file: {main_old}")
        else:
            say(f"main file: {main_old} (old) -> {main_new} (new)")

        old_flat = os.path.join(tmp, "old_flat.tex")
        new_flat = os.path.join(tmp, "new_flat.tex")
        say("flattening...")
        latexpand(old_root, main_old, old_flat)
        latexpand(new_root, main_new, new_flat)

        # ---- Bibliography expansion --------------------------------------
        # latexdiff only sees \cite keys, so an edited .bib entry would never
        # show up. For BibTeX documents we compile each side once to produce
        # its formatted .bbl and re-flatten with the bibliography inlined, so
        # reference changes get marked up like any other text. biblatex is
        # skipped: its .bbl is driver code, not typesettable text.
        bbl_expanded = False
        old_flat_noexp = os.path.join(tmp, "old_flat_noexp.tex")
        new_flat_noexp = os.path.join(tmp, "new_flat_noexp.tex")
        old_bib = bib_kind(old_flat)
        new_bib = bib_kind(new_flat)
        if old_bib == "biblatex" or new_bib == "biblatex":
            say("note: biblatex document; bibliography changes will not show in the diff")
        elif old_bib == "bibtex" or new_bib == "bibtex":
            say("expanding bibliographies (reference changes will show in the diff)...")
            old_bbl = new_bbl = None
            bbl_ok = True
            if old_bib == "bibtex":
                old_bbl = make_bbl(old_root, main_old)
                if old_bbl is None:
                    bbl_ok = False
            if new_bib == "bibtex":
                new_bbl = make_bbl(new_root, main_new)
                if new_bbl is None:
                    bbl_ok = False
            if bbl_ok:
                # Keep the plain flats: the build retries with these if the
                # expanded diff fails to compile.
                shutil.copy(old_flat, old_flat_noexp)
                shutil.copy(new_flat, new_flat_noexp)
                prepare_bbls(
                    os.path.join(old_root, old_bbl) if old_bbl else None,
                    os.path.join(new_root, new_bbl) if new_bbl else None)
                # A side without a bibliography stays plain, so the whole
                # bibliography correctly shows as added (or deleted).
                if old_bbl:
                    latexpand(old_root, main_old, old_flat, "--expand-bbl", old_bbl)
                if new_bbl:
                    latexpand(new_root, main_new, new_flat, "--expand-bbl", new_bbl)
                bbl_expanded = True
            else:
                # All or nothing: expanding one side only would diff the
                # formatted bibliography against \bibliography{...}.
                warn("warning: could not generate a .bbl; bibliography changes will not show in the diff")
        # ---- End bibliography expansion ----------------------------------

        diff_tex = os.path.join(build, "diff.tex")
        latexdiff_log = os.path.join(tmp, "latexdiff.log")

        def run_latexdiff(old_f, new_f, extra):
            with open(diff_tex, "wb") as out, open(latexdiff_log, "ab") as errf:
                return subprocess.run(
                    ["latexdiff", f"--type={diff_type}", "--append-safecmd=label",
                     *extra, old_f, new_f], stdout=out, stderr=errf)

        def diff_written():
            return os.path.isfile(diff_tex) and os.path.getsize(diff_tex) > 0

        say(f"running latexdiff (type={diff_type})...")
        gfx = [GRAPHICS_MARKUP]
        ld = run_latexdiff(old_flat, new_flat, gfx)
        if not diff_written():
            gfx = []                              # latexdiff too old to know it
            ld = run_latexdiff(old_flat, new_flat, gfx)
        if not diff_written():
            warn(f"latexdiff failed (status {ld.returncode}). Log:")
            warn(read_text(latexdiff_log))
            sys.exit(1)

        # Copy every non-main file from the new project into the build dir so
        # figures, .bib, .cls, .sty and the like resolve during compilation.
        for dirpath, _dirs, files in os.walk(new_root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, new_root)
                if rel == main_new:
                    continue
                dst = os.path.join(build, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(full, dst)

        # Figures the new version dropped or renamed: the deleted
        # \includegraphics is live in the diff, so its file must be here too.
        if gfx:
            n_old = copy_old_figures(old_root, old_flat, new_flat, build)
            if n_old:
                say(f"old-only figures: {n_old} copied so deletions render")

        say("building PDF...")
        cur_old, cur_new = old_flat, new_flat
        ok = build_diff_pdf(build, main_new)
        # latexdiff markup inside a formatted bibliography occasionally produces
        # unbuildable TeX; redo the diff from the unexpanded flats (giving up
        # only the bibliography markup) rather than fail.
        if not ok and bbl_expanded:
            warn("warning: build failed with expanded bibliographies; retrying without them")
            cur_old, cur_new = old_flat_noexp, new_flat_noexp
            run_latexdiff(cur_old, cur_new, gfx)
            ok = build_diff_pdf(build, main_new)
        # Deleted-figure markup is the other thing that can make a document
        # unbuildable; shed that too rather than fail.
        if not ok and gfx:
            warn("warning: build failed with deleted-figure markup; retrying without it")
            run_latexdiff(cur_old, cur_new, [])
            build_diff_pdf(build, main_new)

        if not os.path.isfile(os.path.join(build, "diff.pdf")):
            warn("error: PDF build failed. Tail of diff.log:")
            tail_to_stderr(os.path.join(build, "diff.log"), 40)
            sys.exit(1)

        shutil.copy(os.path.join(build, "diff.pdf"), out_pdf)

        say("comparing figures...")
        manifest = compare_figures(old_root, new_root, old_flat, new_flat, tmp)

        if manifest:
            if fig_dir:
                os.makedirs(fig_dir, exist_ok=True)
                for cpath, cname in manifest:
                    shutil.copy(cpath, os.path.join(fig_dir, f"{cname}_diff.png"))
                say(f"wrote figure collages to: {fig_dir}/")

            if embed_figs:
                figures_pdf = os.path.join(tmp, "figures.pdf")
                combined = os.path.join(tmp, "combined.pdf")
                if build_fig_appendix(manifest, figures_pdf, tmp) \
                        and merge_pdfs(out_pdf, figures_pdf, combined):
                    shutil.copy(combined, out_pdf)
                    say(f"appended {len(manifest)} figure collage page(s) to the PDF")
                else:
                    warn("warning: could not embed figure collages (need pdfunite or gs to merge);")
                    warn("         re-run with -c DIR to save them as PNG files instead.")

        say(f"wrote: {out_pdf}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
