#!/usr/bin/env python3
"""latexdiff-zip: produce a latexdiff PDF between two LaTeX project archives.

Two snapshots downloaded from Overleaf history, or two arXiv source tarballs,
become one track-changes PDF -- text *and* figures. Each side may be a local
archive (.zip or a tar: .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz), and
the two sides may use different formats. Either side may instead be an arXiv
paper given as an id or URL (2401.12345v1, arXiv:hep-th/9901001,
https://arxiv.org/abs/...); its source is then fetched from arxiv.org.

Usage: latexdiff-zip [-m main.tex] [-M main.tex] [-o output.pdf] [-t TYPE]
                     [-F] [-L] [-c DIR] OLD NEW
       latexdiff-zip -V

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
import struct
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import urllib.request
import zipfile
import zlib

# The release, in SemVer. The web app reads it from here too, so it is the one
# place to bump; CHANGELOG.md says what each release changed.
__version__ = "2.4.0"

UA = f"latexdiff-zip/{__version__} (+https://github.com/toftul/latexdiff-zip)"
SITE_URL = "https://latexdiff.toftul.net/"
REPO_URL = "https://github.com/toftul/latexdiff-zip"
DEVNULL = subprocess.DEVNULL

# latexdiff's default graphics markup ("new-only") comments deleted
# \includegraphics commands away, so a figure the new version dropped just
# vanishes from the diff. "both" keeps them live -- drawn at reduced scale with
# a red cross -- which is why the build also needs the old files
# (copy_old_figures). Given up on the last retry: latexdiff's own manual warns
# it can provoke "Misplaced \noalign" on some tables.
GRAPHICS_MARKUP = "--graphics-markup=both"

# -t BLUE is ours, not latexdiff's: additions in plain blue, deletions not shown
# at all. latexdiff runs with CFONT and these definitions go in just before
# \begin{document}, overriding its \providecommand ones. (latexdiff's own
# --preamble would be the natural hook, but in the texlive/texlive image it
# dies on a missing Perl module, Encode::Locale.) An empty \DIFdel is not
# enough: a deleted \section stays live with only its title wrapped, leaving a
# bare numbered heading. So \DIFdelbegin swallows everything up to its
# \DIFdelend -- the heading, its %DIFAUXCMD counter fix-up, the lot. Deleted
# figures are dropped too: BLUE keeps latexdiff's default "new-only" graphics
# markup and so never needs copy_old_figures.
BLUE_TYPE = "BLUE"
BLUE_PREAMBLE = rb"""%DIF BLUE PREAMBLE (latexdiff-zip)
\RequirePackage{color}
\DeclareRobustCommand{\DIFadd}[1]{{\color{blue}#1}}
\DeclareRobustCommand{\DIFdel}[1]{}
\long\def\DIFdelbegin#1\DIFdelend{}
\long\def\DIFdelbeginFL#1\DIFdelendFL{}
"""
_BEGIN_DOC_RE = re.compile(rb"^[^\S\n]*\\begin\{document\}", re.M)

# Image extensions tried when resolving an \includegraphics path with no suffix.
# The order is pdflatex's own (pdftex.def's \Gin@extensions, then the .eps that
# epstopdf appends), so when a project ships both plot.pdf and plot.png the file
# compared is the one the paper draws. Formats pdflatex cannot include come last.
RESOLVE_EXTS = ("", ".pdf", ".png", ".jpg", ".mps", ".jpeg", ".jbig2", ".jb2",
                ".PDF", ".PNG", ".JPG", ".JPEG", ".JBIG2", ".JB2", ".eps",
                ".ps", ".svg", ".tiff", ".bmp")

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
        f"Usage: {prog} [-m main.tex] [-M main.tex] [-o output.pdf] [-t TYPE] [-F] [-L] [-c DIR] OLD NEW\n"
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
        "  -t TYPE        latexdiff --type value (default: UNDERLINE), or BLUE:\n"
        "                 additions in blue, deletions not shown at all.\n"
        "  -F             Do not append the figure comparisons to the PDF (on by default).\n"
        "  -L, --no-label Do not print the \"Made with latexdiff-zip\" label at the\n"
        "                 bottom of each page (on by default).\n"
        "  -c DIR         Also save each figure comparison as a PNG in DIR.\n"
        "  -V, --version  Print the version and exit."
    )
    sys.exit(2)


def parse_args(argv):
    main_old = ""
    main_new = ""
    out_pdf = ""
    diff_type = "UNDERLINE"
    embed_figs = True
    page_label = True
    fig_dir = ""
    try:
        opts, rest = getopt.getopt(argv, "m:M:o:t:c:FLhV", ["version", "help", "no-label"])
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
        elif opt in ("-L", "--no-label"):
            page_label = False
        elif opt in ("-h", "--help"):
            usage()
        elif opt in ("-V", "--version"):
            say(f"latexdiff-zip {__version__}")
            sys.exit(0)
    if len(rest) != 2:
        usage()
    return {
        "main_old": main_old, "main_new": main_new, "out_pdf": out_pdf,
        "diff_type": diff_type, "embed_figs": embed_figs, "page_label": page_label,
        "fig_dir": fig_dir,
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
    if "--expand-bbl" in extra:
        _comment_out_commented_bbls(out_path)


# An unescaped % starts a comment: one after an even number of backslashes
# (\% is a literal percent sign; \\% is a line break, then a comment).
_COMMENT_RE = re.compile(r"(?<!\\)((?:\\\\)*)%.*")
_COMMENT_START_RE = re.compile(rb"(?<!\\)(?:\\\\)*%")


def _strip_comments(text):
    """Remove TeX comments and keep the line breaks. The flats keep comments
    (latexpand --keep-comments), but a commented-out command is not part of
    the document."""
    return _COMMENT_RE.sub(r"\1", text)


def _comment_out_commented_bbls(path):
    """With --keep-comments, latexpand --expand-bbl also expands a commented
    \\bibliography, and only the first line of the inserted bibliography is in
    the comment. The rest is live: a second reference list after the real one.
    Comment out each such copy up to its \\end{thebibliography}. Works on bytes
    so a non-UTF-8 source survives."""
    try:
        with open(path, "rb") as f:
            lines = f.read().split(b"\n")
    except OSError:
        return
    inside = False
    for i, line in enumerate(lines):
        if inside:
            lines[i] = b"%" + line
            if b"\\end{thebibliography}" in line:
                inside = False
            continue
        at = line.find(b"\\begin{thebibliography}")
        if at >= 0 and _COMMENT_START_RE.search(line[:at]) \
                and b"\\end{thebibliography}" not in line[at:]:
            inside = True
    with open(path, "wb") as f:
        f.write(b"\n".join(lines))


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
_CAPTION_RE = re.compile(r"\\caption(?![a-zA-Z@])\s*(?:\[[^\]]*\])?")
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
    """Convert any supported image to a flat 8-bit PNG (no alpha, white
    background). 8-bit: ImageMagick otherwise keeps 16 bits per channel,
    doubling the size for nothing a screen can show."""
    ext = os.path.splitext(src)[1].lstrip(".").lower()
    if ext in ("pdf", "eps", "ps"):
        return _im(["-density", "150", f"{src}[0]", "-background", "white",
                    "-alpha", "remove", "-flatten", "-depth", "8", dst])
    return _im(["-background", "white", src, "-alpha", "remove", "-flatten",
                "-depth", "8", dst])


def png_size(path):
    """(width, height) of a PNG, read from its IHDR chunk, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w and h else None


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
    text = _strip_comments(text)
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
    text = _strip_comments(text)                  # commented-out figures don't count
    # Pair each \begin with its \end. A graphic takes the label of the
    # innermost figure environment around it, and no label when it is
    # outside every figure (not the label of the figure before it).
    events = sorted([(m.start(), True) for m in _FIGBEG_RE.finditer(text)]
                    + [(m.start(), False) for m in _FIGEND_RE.finditer(text)])
    spans, stack = [], []
    for p, is_begin in events:
        if is_begin:
            stack.append(p)
        elif stack:
            spans.append((stack.pop(), p))
    # Each ref also records its environment's caption (the long form, as
    # TeX source), whether the label comes after the caption -- only then
    # does \ref give the figure number; before it, LaTeX's classic slip, it
    # gives the section's -- and which panel of the environment it is.
    refs, by_env = [], {}
    for igm in _IG_RE.finditer(text):
        pos = igm.start()
        env = max((sp for sp in spans if sp[0] < pos < sp[1]),
                  key=lambda sp: sp[0], default=None)
        label, caption, numbered = "", "", False
        if env is not None:
            body = text[env[0]:env[1]]
            lbls = list(_LABEL_RE.finditer(body))
            label = lbls[0].group(1) if lbls else ""
            cm = _CAPTION_RE.search(body)
            if cm:
                caption = _read_group(body, cm.end())[0] or ""
                numbered = bool(lbls) and lbls[0].start() > cm.start()
        ref = {"label": label, "path": igm.group(1).strip(), "pos": pos,
               "caption": caption, "numbered": numbered, "panel": (1, 1)}
        refs.append(ref)
        if env is not None:
            by_env.setdefault(env, []).append(ref)
    for group in by_env.values():
        for i, ref in enumerate(group):
            ref["panel"] = (i + 1, len(group))
    return refs


def _fig_name(ref):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", ref["label"] or ref["path"])


def match_figures(old_root, new_root, old_flat, new_flat):
    """Pair figures between the two versions by \\label first, then by file
    path, then (unlabelled figures only) by document order. Returns one list
    of figure changes in document order, each a dict:

        status    "changed" (paired but byte-different), "added" (only in the
                  new version) or "removed" (only in the old version)
        name      file-name-safe identifier (label, else path)
        old, new  the resolved files, None on the side that lacks it
        old_ref, new_ref  the parsed \\includegraphics refs, likewise

    Changed and added figures sit where they appear in the new version; a
    removed figure goes right after the figure that preceded it in the old
    version (unchanged pairs included), so the list reads like the paper.

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

    # Leftovers. The same file named on both sides is one figure whose \label
    # was added, dropped or renamed. After that, pair by document order only
    # figures with no \label on either side: a labelled leftover is a figure
    # the other version lacks, and pairing it by position would set it
    # against an unrelated image.
    old_left = [r for r in old_refs if id(r) not in old_used]
    new_left = []
    for nr in unmatched_new:
        twin = next((r for r in old_left if r["path"] == nr["path"]), None)
        if twin is None:
            new_left.append(nr)
        else:
            matched.append((twin, nr))
            old_left = [r for r in old_left if r is not twin]
    old_plain = [r for r in old_left if not r["label"]]
    new_plain = [r for r in new_left if not r["label"]]
    k = min(len(old_plain), len(new_plain))
    matched.extend(zip(old_plain[:k], new_plain[:k]))
    paired = {id(r) for r in old_plain[:k] + new_plain[:k]}
    added_refs = [r for r in new_left if id(r) not in paired]
    removed_refs = [r for r in old_left if id(r) not in paired]

    # Order: new-version position for paired and added figures; a removed
    # figure is keyed just after the new position of the nearest earlier
    # old figure that was paired (or first, if none was).
    partner = {id(or_): nr for or_, nr in matched}
    order = {id(nr): (nr["pos"], 0, 0) for nr in new_refs}
    anchor = -1
    for or_ in old_refs:
        if id(or_) in partner:
            anchor = partner[id(or_)]["pos"]
        else:
            order[id(or_)] = (anchor, 1, or_["pos"])

    entries = []
    for or_, nr in matched:
        old_file = _resolve(or_["path"], old_root, old_gp)
        new_file = _resolve(nr["path"], new_root, new_gp)
        if old_file is None or new_file is None:
            continue
        if _md5(old_file) == _md5(new_file):
            continue
        entries.append({"status": "changed", "name": _fig_name(nr),
                        "old": old_file, "new": new_file,
                        "old_ref": or_, "new_ref": nr, "key": order[id(nr)]})
    for nr in added_refs:
        f = _resolve(nr["path"], new_root, new_gp)
        if f is not None:
            entries.append({"status": "added", "name": _fig_name(nr),
                            "old": None, "new": f,
                            "old_ref": None, "new_ref": nr, "key": order[id(nr)]})
    for or_ in removed_refs:
        f = _resolve(or_["path"], old_root, old_gp)
        if f is not None:
            entries.append({"status": "removed", "name": _fig_name(or_),
                            "old": f, "new": None,
                            "old_ref": or_, "new_ref": None, "key": order[id(or_)]})
    entries.sort(key=lambda e: e["key"])
    return entries


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


def compare_figures(old_root, new_root, old_flat, new_flat, tmp, labels=None):
    """Return the figure changes to show, in document order: match_figures'
    entries with each present side rasterised ("old_png"/"new_png") and its
    shape recorded ("old_aspect"/"new_aspect", width over height). Edited pairs
    whose rasterised pixels are identical -- only the file bytes changed -- are
    dropped. `labels` (read_aux_labels of the diff build) adds each figure's
    number and page (annotate_figures). Degrades to an empty list (with a
    warning) when ImageMagick is absent."""
    if not (which("magick") or which("convert")):
        warn("warning: ImageMagick not found; skipping figure comparison")
        return []
    figcmp = os.path.join(tmp, "figcmp")
    os.makedirs(figcmp, exist_ok=True)

    shown = []
    counts = {"changed": 0, "added": 0, "removed": 0}
    entries = match_figures(old_root, new_root, old_flat, new_flat)
    annotate_figures(entries, labels or {})
    for i, e in enumerate(entries):
        ok = True
        for side in ("old", "new"):
            e[side + "_png"] = e[side + "_aspect"] = None
            if e[side] is None:
                continue
            png = os.path.join(figcmp, f"{i:03d}_{side}.png")
            size = png_size(png) if to_png(e[side], png) else None
            if size is None:
                warn(f"  warning: cannot convert {e[side]}")
                ok = False
                break
            e[side + "_png"] = png
            e[side + "_aspect"] = size[0] / size[1]
        if not ok:
            continue
        if e["status"] == "changed" and _visually_identical(e["old_png"], e["new_png"]):
            continue                               # same pixels, only bytes changed
        where = ", ".join(x for x in (e["number"] and f"Figure {e['number']}",
                                      e["page"] and f"p. {e['page']}") if x)
        say(f"  figure {e['status']}: {e['name']}" + (f" ({where})" if where else ""))
        counts[e["status"]] += 1
        shown.append(e)

    parts = [f"{n} {status}" for status, n in counts.items() if n]
    if parts:
        say("figure diff: " + ", ".join(parts) + " figure(s)")
    else:
        say("figure diff: no figure changes detected")
    return shown


# ---- figure appendix ------------------------------------------------------
#
# The figure changes are typeset as a LaTeX document of their own and appended
# to the diff PDF (merge_pdfs), not drawn as ImageMagick collages: the labels
# are real text -- searchable, selectable, sharp at any zoom -- and vector
# figures stay vector. Accessibility rules the page keeps:
#
# - Colour is never the only cue. Each panel is named OLD or NEW in words, and
#   OLD always comes first (left, or on top); each status is a word plus a
#   symbol. A greyscale print loses nothing.
# - The side colours are the text diff's own (latexdiff draws deletions red and
#   additions blue), darkened to 7.5:1 on white; the grey text is 6:1.
# - \pdfgentounicode makes copied and extracted text come out as typed.
#
# Comparisons are packed in document order: each is one unbreakable block, and
# LaTeX starts a new page when the next block does not fit.

APPENDIX_MARGIN = 42.52           # bp (1.5 cm), all four sides
PANEL_H = 0.30                    # tallest panel, as a fraction of \textheight
SIDE_W = 0.485                    # panel width side by side, fraction of \linewidth
A4 = (595.28, 841.89)             # bp
_A5_SHORT, _A5_LONG = 419.53, 595.28
_A3_SHORT, _A3_LONG = 841.89, 1190.55

# pdflatex includes these directly (.eps through epstopdf); anything else goes
# in as the rasterised PNG.
APPENDIX_NATIVE = (".pdf", ".png", ".jpg", ".jpeg", ".eps")

_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]")
_STREAM_RE = re.compile(rb"(?<!end)stream\r?\n")


def pdf_page_size(path):
    """(width, height) in bp of a PDF's first /MediaBox, or None. pdfTeX keeps
    its page objects in compressed object streams, so those are inflated
    (zlib) when the plain bytes hold no /MediaBox."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    m = _MEDIABOX_RE.search(data)
    if m is None:
        for sm in _STREAM_RE.finditer(data):
            if b"/ObjStm" not in data[max(0, sm.start() - 300):sm.start()]:
                continue
            end = data.find(b"endstream", sm.end())
            if end < 0:
                break
            try:
                chunk = zlib.decompressobj().decompress(data[sm.end():end])
            except zlib.error:
                continue
            m = _MEDIABOX_RE.search(chunk)
            if m:
                break
    if m is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    w, h = abs(x1 - x0), abs(y1 - y0)
    return (w, h) if w > 0 and h > 0 else None


def appendix_page_size(size):
    """The appendix's page size for a paper of `size` (bp, or None). A normal
    document size -- A5 up to A3, either orientation, letter included -- is
    kept, so a printed diff has one page size throughout. Anything else (an
    8x10 cm demo, beamer slides, an A0 poster) gets A4 in the paper's
    orientation: the comparisons need room, and a poster's would be huge."""
    if not size:
        return A4
    w, h = size
    short, long_ = sorted((w, h))
    if _A5_SHORT - 1 <= short <= _A3_SHORT + 1 and _A5_LONG - 1 <= long_ <= _A3_LONG + 1:
        return (w, h)
    return (A4[1], A4[0]) if w > h else A4


def _fit(aspect, box_w, box_h):
    """Size of an image of the given aspect (w/h) scaled to fit box_w x box_h."""
    if box_w / aspect <= box_h:
        return box_w, box_w / aspect
    return box_h * aspect, box_h


def choose_layout(aspects, line_w, text_h):
    """'side' (OLD left of NEW) or 'stack' (OLD above NEW): whichever shows the
    present panels larger. Every panel is capped at PANEL_H of the text height,
    so the difference is only the width: wide figures stack, tall and square
    ones sit side by side (the more compact of the two on a tie)."""
    box_h = PANEL_H * text_h

    def area(box_w):
        return sum(w * h for w, h in (_fit(a, box_w, box_h) for a in aspects))

    return "stack" if area(line_w) > area(SIDE_W * line_w) * 1.001 else "side"


_TEX_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "‘": "`", "’": "'", "“": "``", "”": "''",
    "–": "--", "—": "---", "…": r"\ldots{}", " ": "~",
}


def tex_escape(text):
    """Plain text as LaTeX source that typesets it verbatim. Characters beyond
    Latin-1 and Latin Extended-A, which the appendix's T1 fonts cannot draw,
    become '?': a file name in Cyrillic must not cost the whole page."""
    out = []
    for ch in text:
        if ch in _TEX_ESCAPES:
            out.append(_TEX_ESCAPES[ch])
        elif " " <= ch <= "~" or "¡" <= ch <= "ſ":
            out.append(ch)
        elif ch in "\t\n\r":
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


def _ref_of(e):
    """The ref an entry is described by: NEW's, except for a removed figure."""
    return e["old_ref"] if e["status"] == "removed" else e["new_ref"]


def entry_ident(e):
    """What a comparison is called: "Figure N" when the number is certain,
    else the figure's \\label, else its file as written; plus "panel i of n"
    for one image of a multi-image figure."""
    ref = _ref_of(e)
    ident = f"Figure {e['number']}" if e.get("number") else (ref["label"] or ref["path"])
    i, n = ref.get("panel", (1, 1))
    return ident + (f" \u00b7 panel {i} of {n}" if n > 1 else "")


# ---- captions as plain text -----------------------------------------------
#
# A caption is quoted in the appendix as plain text: commands are dropped (an
# argument-taking one keeps its last argument's text), maths stays as its TeX
# source. Typesetting the real caption would need the paper's own macros.

_MATH_RE = re.compile(r"\$\$.+?\$\$|(?<!\\)\$(?:\\.|[^$\\])+\$|\\\(.+?\\\)|\\\[.+?\\\]", re.S)
_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308",
            "~": "\u0303", "=": "\u0304", ".": "\u0307", "c": "\u0327",
            "v": "\u030c", "u": "\u0306", "H": "\u030b", "r": "\u030a"}
_CMD_WORDS = {"ldots": "\u2026", "dots": "\u2026", "textellipsis": "\u2026",
              "LaTeX": "LaTeX", "TeX": "TeX", "ss": "\u00df", "textendash": "\u2013",
              "textemdash": "\u2014", "S": "\u00a7", "P": "\u00b6"}
_CMD_DROP = {"label", "footnote", "footnotemark", "index", "protect", "centering",
             "vspace", "hspace", "phantom", "linebreak", "newline", "nocite",
             "hfill", "vfill", "noindent", "relax"}
_CMD_CITE = {"cite", "citep", "citet", "citealp", "citealt", "citeauthor",
             "citeyear", "parencite", "textcite", "autocite", "footcite"}
_CMD_REF = {"ref", "eqref", "autoref", "cref", "Cref", "pageref", "vref",
            "subref", "nameref"}
_CTRL_RE = re.compile(r"\\([a-zA-Z@]+)\*?|\\(.)", re.S)


def _tex_to_text(s):
    """TeX source (no maths) to the text it would print, roughly."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "{}":
            i += 1
            continue
        if c == "~":
            out.append(" ")
            i += 1
            continue
        if c != "\\":
            out.append(c)
            i += 1
            continue
        m = _CTRL_RE.match(s, i)
        if m is None:                              # a lone trailing backslash
            break
        i = m.end()
        name = m.group(1) or m.group(2)
        if name in _ACCENTS and (m.group(2) or name in "cvuHr"):
            arg, j = _read_group(s, i)
            if arg is None:
                arg, j = s[i:i + 1], i + 1
            base = _tex_to_text(arg)
            if base:
                out.append(unicodedata.normalize("NFC", base[0] + _ACCENTS[name]) + base[1:])
            i = j
            continue
        if m.group(2) is not None:                 # \% \& \_ ... or a spacing command
            out.append(name if name in "%&_#${}" else " ")
            continue
        groups = []                                # optional [..] then {..} arguments
        while True:
            k = i
            while k < n and s[k] in " \t\n":
                k += 1
            if k < n and s[k] == "[" and not groups:
                close = s.find("]", k)
                if close < 0:
                    break
                i = close + 1
                continue
            if k < n and s[k] == "{":
                g, j = _read_group(s, k)
                if g is None:
                    break
                groups.append(g)
                i = j
                continue
            break
        if name in _CMD_WORDS:
            out.append(_CMD_WORDS[name])
        elif name in _CMD_CITE:
            out.append("[\u2026]")
        elif name in _CMD_REF:
            out.append("\u2026")
        elif name not in _CMD_DROP and groups:
            out.append(_tex_to_text(groups[-1]))
    return "".join(out)


def caption_segments(raw, limit=100):
    """A caption as [(kind, text)], kind "text" or "math" (TeX source),
    whitespace collapsed and cut to about `limit` characters at a word
    boundary, with an ellipsis. Maths is never cut in half."""
    parts, pos = [], 0
    for m in _MATH_RE.finditer(raw):
        parts += [("text", _tex_to_text(raw[pos:m.start()])),
                  ("math", " ".join(m.group(0).split()))]
        pos = m.end()
    parts.append(("text", _tex_to_text(raw[pos:])))

    segs, total = [], 0
    for kind, t in parts:
        if kind == "text":
            t = re.sub(r"\s+", " ", t)
            if not segs:
                t = t.lstrip()
        if not t:
            continue
        if total + len(t) > limit:
            room = t[:max(0, limit - total)] if kind == "text" else ""
            if " " in room:
                room = room.rsplit(" ", 1)[0]
            if not room.strip() and segs and segs[-1][0] == "text":
                segs[-1] = ("text", segs[-1][1].rstrip() + "\u2026")
            else:
                segs.append(("text", room.rstrip() + "\u2026"))
            return segs
        segs.append((kind, t))
        total += len(t)
    if segs and segs[-1][0] == "text":
        segs[-1] = ("text", segs[-1][1].rstrip())
        if not segs[-1][1]:
            segs.pop()
    return segs


def _caption_tex(segs):
    return "".join(tex_escape(t) if kind == "text" else r"\ldzmath{" + tex_escape(t) + "}"
                   for kind, t in segs)


# ---- figure numbers and pages from the diff's .aux -------------------------

_NEWLABEL_RE = re.compile(r"\\newlabel\{([^}]*)\}")
_MULTIPLY_RE = re.compile(r"Label `([^']*)' multiply defined")
_AUX_VALUE_RE = re.compile(r"[\w.\-]+")


def read_aux_labels(aux_path, log_path):
    """{label: (number, page)} from a LaTeX .aux's \\newlabel lines. number is
    '' outside a numbered environment; page is the printed page (\\thepage).
    Left out, because neither can be trusted: a label the log reports as
    multiply defined (latexdiff can merge two float environments, and then the
    last definition wins), and any value with macros in it."""
    text = read_text(aux_path)
    multiply = set(_MULTIPLY_RE.findall(read_text(log_path)))
    labels = {}
    for m in _NEWLABEL_RE.finditer(text):
        if m.group(1) in multiply:
            continue
        outer, _ = _read_group(text, m.end())
        if outer is None:
            continue
        number, p = _read_group(outer, 0)
        page = _read_group(outer, p)[0] if number is not None else None
        if page is None:
            continue
        number, page = number.strip(), page.strip()
        if not _AUX_VALUE_RE.fullmatch(page) or (number and not _AUX_VALUE_RE.fullmatch(number)):
            continue
        labels[m.group(1)] = (number, page)
    return labels


def annotate_figures(entries, labels):
    """Give each entry what the appendix says about it besides the images:
    "number" (only for a changed or added figure whose \\label follows its
    \\caption -- a removed figure's label sits in deleted markup, where \\ref
    gives the section number), "page" in the diff (whenever its label is in
    the .aux), and the "caption" source."""
    for e in entries:
        ref = _ref_of(e)
        number, page = labels.get(ref["label"], ("", None)) if ref["label"] else ("", None)
        e["number"] = number if (number and ref.get("numbered")
                                 and e["status"] != "removed") else None
        e["page"] = page
        e["caption"] = ref.get("caption", "")


# Preamble of the designed page ("full" and "png" levels). @W@, @H@ and @M@ are
# the page width, height and margin in bp.
_APPENDIX_PREAMBLE = r"""\documentclass[10pt]{article}
\usepackage[paperwidth=@W@bp,paperheight=@H@bp,margin=@M@bp,footskip=@F@bp]{geometry}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage{tgheros}
\renewcommand{\familydefault}{\sfdefault}
\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{tikz}
\usepackage{tabularx}
\usepackage[hidelinks,pdfpagelabels=false,bookmarksopen=true]{hyperref}
\usepackage{bookmark}
% No Info fields of its own: in the merge, gs lets the later file's fields win,
% and hyperref's empty /Title and /Author would erase the paper's.
\makeatletter\let\pdfinfo\@gobble\makeatother
\input{glyphtounicode}
\pdfgentounicode=1
\definecolor{ldztext}{HTML}{202124}
\definecolor{ldzmuted}{HTML}{5F6368}
\definecolor{ldzold}{HTML}{A61B1B}
\definecolor{ldznew}{HTML}{0B4FB3}
\definecolor{ldzchg}{HTML}{3C4043}
\definecolor{ldzframe}{HTML}{9AA0A6}
\definecolor{ldzdash}{HTML}{80868B}
\newcommand{\ldzchip}[2]{\tikz[baseline=(c.base)]\node[draw=#1,text=#1,
  line width=0.8bp,rounded corners=2bp,inner xsep=3.5bp,inner ysep=2bp,
  font=\small\bfseries\boldmath](c){#2};}
\newcommand{\ldzside}[2]{{\color{#1}\rule[-0.3ex]{2.5bp}{2ex}}\hspace{0.5em}%
  {\small\bfseries\color{#1}#2}}
\newcommand{\ldzimg}[3]{{\setlength{\fboxsep}{0pt}\setlength{\fboxrule}{0.4bp}%
  \fcolorbox{ldzframe}{white}{\includegraphics[width=#1,height=#2,keepaspectratio]{#3}}}}
\newcommand{\ldzabsent}[3]{\tikz\node[draw=ldzdash,dashed,line width=0.6bp,
  rounded corners=3bp,minimum width=#1,minimum height=#2,inner sep=0pt,
  text width=#1,align=center,text=ldzmuted,font=\small\itshape]{#3};}
\newcommand{\ldzfoot}{{\footnotesize\color{ldzmuted}Figure changes
  \enspace\textperiodcentered\enspace\thepage}}
\newcommand{\ldzrule}{\noalign{\vspace{3bp}{\color{ldzframe}\hrule height 0.4bp}\vspace{4bp}}}
"""

# Preamble of the "minimal" level: base LaTeX only (no font packages, no
# TikZ), the same macro names, so the body is shared.
_APPENDIX_PREAMBLE_MINIMAL = r"""\documentclass[10pt]{article}
\usepackage[paperwidth=@W@bp,paperheight=@H@bp,margin=@M@bp,footskip=@F@bp]{geometry}
\usepackage{graphicx}
\usepackage{color}
\definecolor{ldztext}{rgb}{0,0,0}
\definecolor{ldzmuted}{rgb}{0.37,0.39,0.41}
\definecolor{ldzold}{rgb}{0.65,0.11,0.11}
\definecolor{ldznew}{rgb}{0.04,0.31,0.70}
\definecolor{ldzchg}{rgb}{0.24,0.25,0.26}
\newcommand{\ldzchip}[2]{\textbf{\textcolor{#1}{#2}}}
\newcommand{\ldzside}[2]{\textbf{\textcolor{#1}{#2}}}
\newcommand{\ldzimg}[3]{\includegraphics[width=#1,height=#2,keepaspectratio]{#3}}
\newcommand{\ldzabsent}[3]{\fbox{\parbox[c][#2][c]{#1}{\centering\itshape #3}}}
\newcommand{\ldzfoot}{Figure changes, page \thepage}
\usepackage{tabularx}
\providecommand{\hyperlink}[2]{#2}
\providecommand{\hypertarget}[2]{#2}
\providecommand{\pdfbookmark}[3][]{}
\newcommand{\ldzrule}{\noalign{\vspace{2bp}}\hline\noalign{\vspace{3bp}}}
"""

_APPENDIX_COMMON = r"""\setlength{\parindent}{0pt}
\raggedbottom
\renewcommand{\thepage}{A\arabic{page}}
\makeatletter
\renewcommand{\@oddfoot}{\hfil\ldzfoot\hfil}
\renewcommand{\@evenfoot}{\@oddfoot}
\makeatother
\newenvironment{ldzblock}{\par\noindent\begin{minipage}{\linewidth}}{\end{minipage}\par}
\newcommand{\ldzchanged}{\ldzchip{ldzchg}{$\Delta$\,Changed}}
\newcommand{\ldzadded}{\ldzchip{ldznew}{$+$\,Added}}
\newcommand{\ldzremoved}{\ldzchip{ldzold}{$-$\,Removed}}
\newcommand{\ldzmath}[1]{{\upshape\ttfamily\small #1}}
\newcommand{\ldzmeta}[1]{\enspace{\color{ldzmuted}\textperiodcentered\enspace #1}}
\newcommand{\ldzcaption}[1]{{\rmfamily\itshape #1\par}}
\newcommand{\ldzhead}[1]{\leavevmode{\small\color{ldzmuted}#1}}
"""

_STATUS_MACRO = {"changed": r"\ldzchanged", "added": r"\ldzadded",
                 "removed": r"\ldzremoved"}
_STATUS_ORDER = ("changed", "added", "removed")


def _bp(x):
    return f"{x:.2f}bp"


def _appendix_block(i, e, files, layout, line_w, text_h):
    """LaTeX for one comparison: status and title, then OLD and NEW panels in
    the given layout. `files` maps "old"/"new" to the staged include name (None
    for the missing side of an added or removed figure)."""
    box_h = PANEL_H * text_h
    box_w = SIDE_W * line_w if layout == "side" else line_w
    present = "new" if e["status"] == "added" else "old"
    fit_w, fit_h = _fit(e[present + "_aspect"], box_w, box_h)

    def panel(side):
        colour, word = ("ldzold", "OLD") if side == "old" else ("ldznew", "NEW")
        head = rf"\ldzside{{{colour}}}{{{word}}}"
        if files[side] is not None:
            # 1bp less than the box: the frame is drawn outside the image.
            body = rf"\ldzimg{{{_bp(box_w - 1)}}}{{{_bp(box_h - 1)}}}{{{files[side]}}}"
        else:
            missing = f"Not in {word} version"
            ph = fit_h if layout == "side" else 3 * 12.0
            body = rf"\ldzabsent{{{_bp(fit_w)}}}{{{_bp(ph)}}}{{{missing}}}"
        return head + "\\par\\vspace{3bp}\n" + body

    ident = entry_ident(e)
    head = _STATUS_MACRO[e["status"]] + r"\enspace{\bfseries " + tex_escape(ident) + "}"
    if e.get("page"):
        head += r"\ldzmeta{p.~" + tex_escape(e["page"]) + " in the diff}"
    lines = [
        r"\begin{ldzblock}",
        rf"\hypertarget{{ldz{i}}}{{}}\label{{ldz{i}}}%",
        rf"\pdfbookmark[1]{{{tex_escape(ident)} \textperiodcentered\ {e['status']}}}{{ldzbm{i}}}%",
        head + r"\par",
    ]
    segs = caption_segments(e.get("caption") or "")
    if segs:
        lines.append(r"\vspace{2bp}\ldzcaption{" + _caption_tex(segs) + "}")
    lines.append(r"\vspace{6bp}")
    if layout == "side":
        lines += [
            rf"\begin{{minipage}}[t]{{{_bp(box_w)}}}" + panel("old") + r"\end{minipage}\hfill",
            rf"\begin{{minipage}}[t]{{{_bp(box_w)}}}" + panel("new") + r"\end{minipage}",
        ]
    else:
        lines += [panel("old") + "\\par\\vspace{9bp}", panel("new")]
    lines.append(r"\end{ldzblock}")
    return "\n".join(lines)


def _appendix_overview(entries):
    """Title, totals and a table with one row per comparison: status, figure
    (linked to its comparison), its page in the diff, and its appendix page.
    The top bookmark sits here too."""
    counts = {s: sum(e["status"] == s for e in entries) for s in _STATUS_ORDER}
    summary = r"\enspace\textperiodcentered\enspace ".join(
        f"{counts[s]} {s}" for s in _STATUS_ORDER if counts[s])
    rows = []
    for i, e in enumerate(entries):
        what = tex_escape(entry_ident(e))
        segs = caption_segments(e.get("caption") or "", limit=70)
        if segs:
            what += " --- " + _caption_tex(segs)
        page = r"p.~" + tex_escape(e["page"]) if e.get("page") else "--"
        rows.append(rf"{_STATUS_MACRO[e['status']]} & \hyperlink{{ldz{i}}}{{{what}}} & "
                    rf"{page} & \pageref{{ldz{i}}}\\[2bp]")
    return "\n".join([
        r"\pdfbookmark[0]{Figure changes}{ldztop}%",
        r"{\Large\bfseries Figure changes}\par\vspace{3bp}",
        r"{\color{ldzmuted}" + summary + r"}\par\vspace{10bp}",
        r"\begin{tabularx}{\linewidth}{@{}l>{\raggedright\arraybackslash}Xll@{}}",
        r"\ldzhead{Status} & \ldzhead{Figure} & \ldzhead{In the diff} & \ldzhead{Comparison}\\",
        r"\ldzrule",
        *rows,
        r"\end{tabularx}\par\vspace{22bp}",
    ])


def _stage_figures(entries, figapp, originals):
    """Copy each entry's figures into the appendix dir under short,
    underscore-free names (graphicx reads them verbatim); the original file when
    `originals` and pdflatex can include it, the rasterised PNG otherwise.
    Returns one {"old": name|None, "new": name|None} per entry."""
    staged = []
    for i, e in enumerate(entries):
        files = {}
        for side in ("old", "new"):
            if e[side] is None:
                files[side] = None
                continue
            ext = os.path.splitext(e[side])[1].lower()
            src = e[side] if originals and ext in APPENDIX_NATIVE else e[side + "_png"]
            name = f"f{i:03d}{side[0]}{os.path.splitext(src)[1].lower()}"
            shutil.copy(src, os.path.join(figapp, name))
            files[side] = name
        staged.append(files)
    return staged


def appendix_tex(entries, staged, page, level, single=False, label=True):
    """The appendix as LaTeX source. `level` is "full" or "png" (the designed
    page; the two differ only in what _stage_figures copied) or "minimal" (base
    LaTeX only). `single` puts each comparison on a page of its own with
    nothing else, for the -c export to crop. `label` adds the page label
    (stamp_hook); -L turns it off."""
    w, h = page
    line_w = w - 2 * APPENDIX_MARGIN
    text_h = h - 2 * APPENDIX_MARGIN
    pre = _APPENDIX_PREAMBLE_MINIMAL if level == "minimal" else _APPENDIX_PREAMBLE
    # footskip puts the "Figure changes · A1" footer on the baseline of the
    # page label (stamp_hook), so the two read as one line.
    pre = (pre.replace("@W@", f"{w:.2f}").replace("@H@", f"{h:.2f}")
           .replace("@M@", f"{APPENDIX_MARGIN:.2f}")
           .replace("@F@", f"{APPENDIX_MARGIN - STAMP_BASELINE:.2f}"))
    out = [pre + _APPENDIX_COMMON]
    if single:
        out.append(r"\pagestyle{empty}")       # cropped PNGs: no footer, no label
    elif label:
        out.append(stamp_hook())
    out += [r"\begin{document}", r"\color{ldztext}"]
    if not single:
        out.append(_appendix_overview(entries))
    for i, (e, files) in enumerate(zip(entries, staged)):
        aspects = [e[s + "_aspect"] for s in ("old", "new") if e[s + "_aspect"]]
        layout = choose_layout(aspects, line_w, text_h)
        out.append(_appendix_block(i, e, files, layout, line_w, text_h))
        out.append(r"\clearpage" if single else r"\vspace{20bp plus 8bp minus 6bp}")
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


def _compile_clean(figapp, job):
    """Run pdflatex twice (the second pass settles references) and report
    whether a PDF came out with no error in the log. A nonstopmode run carries
    on past most errors, so a PDF alone does not mean a correct page."""
    for _ in range(2):
        subprocess.run(["pdflatex", "-interaction=nonstopmode", job + ".tex"],
                       cwd=figapp, stdout=DEVNULL, stderr=DEVNULL)
    log = read_text(os.path.join(figapp, job + ".log"))
    return (os.path.isfile(os.path.join(figapp, job + ".pdf"))
            and not re.search(r"^! ", log, re.M))


APPENDIX_LEVELS = ("full", "png", "minimal")


def build_fig_appendix(entries, figapp, page, label=True):
    """Typeset the appendix; return (pdf_path, level), or (None, None). Falls
    back a level at a time: the designed page with the original figure files,
    then with the rasterised PNGs (a figure pdflatex chokes on), then base
    LaTeX only (a missing font or TikZ)."""
    for level in APPENDIX_LEVELS:
        d = os.path.join(figapp, level)
        os.makedirs(d, exist_ok=True)
        staged = _stage_figures(entries, d, originals=(level == "full"))
        with open(os.path.join(d, "figures.tex"), "w", encoding="utf-8") as f:
            f.write(appendix_tex(entries, staged, page, level, label=label))
        if _compile_clean(d, "figures"):
            if level != "full":
                warn(f"warning: figure appendix fell back to the '{level}' style")
            return os.path.join(d, "figures.pdf"), level
    return None, None


def _unique_names(entries):
    """One file-name stem per entry: the entry's name, plus -p<i> for panel i
    of a multi-image figure, and -2, -3, ... if stems still collide."""
    seen, names = {}, []
    for e in entries:
        i, total = _ref_of(e).get("panel", (1, 1)) if "status" in e else (1, 1)
        stem = e["name"] + (f"-p{i}" if total > 1 else "")
        n = seen[stem] = seen.get(stem, 0) + 1
        names.append(stem if n == 1 else f"{stem}-{n}")
    return names


def export_fig_pngs(entries, figapp, page, level, fig_dir):
    """-c DIR: one tightly cropped <name>_diff.png per comparison, the same
    design as the appendix. The comparisons are typeset one per page, then
    each page is rasterised and trimmed to its content. Returns how many
    were written."""
    d = os.path.join(figapp, "export")
    os.makedirs(d, exist_ok=True)
    staged = _stage_figures(entries, d, originals=(level == "full"))
    with open(os.path.join(d, "blocks.tex"), "w", encoding="utf-8") as f:
        f.write(appendix_tex(entries, staged, page, level, single=True))
    _compile_clean(d, "blocks")
    if not os.path.isfile(os.path.join(d, "blocks.pdf")):
        return 0
    if not _im(["-density", "200", os.path.join(d, "blocks.pdf"),
                "-background", "white", "-alpha", "remove", "-alpha", "off",
                "-trim", "+repage", "-bordercolor", "white", "-border", "32",
                "-depth", "8", os.path.join(d, "block-%03d.png")]):
        return 0
    os.makedirs(fig_dir, exist_ok=True)
    n = 0
    for i, stem in enumerate(_unique_names(entries)):
        png = os.path.join(d, f"block-{i:03d}.png")
        if os.path.isfile(png):
            shutil.copy(png, os.path.join(fig_dir, f"{stem}_diff.png"))
            n += 1
    return n


# Ghostscript re-encodes every image as JPEG by default, the paper's own
# figures included. These settings keep JPEGs byte-for-byte and everything
# else lossless (Flate), at full resolution. pdfwrite cannot write 16-bit
# images, so those come out 8-bit -- invisible on screen.
GS_LOSSLESS = [
    "-dAutoFilterColorImages=false", "-dColorImageFilter=/FlateEncode",
    "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/FlateEncode",
    "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
    "-dDownsampleMonoImages=false",
    "-dPassThroughJPEGImages=true", "-dPassThroughJPXImages=true",
]


def merge_pdfs(a, b, out):
    """Concatenate PDFs a and b into out with whichever tool is available.

    Ghostscript first: it keeps both documents' bookmarks and internal links,
    shifting the second one's page targets. pdfunite drops the outline, the
    Info fields (title, author, Creator) and points every internal link at
    page 1 -- a hyperref paper's own links included -- so it is only the
    fallback. gs is slower (about 12 s for a
    100-page paper against well under a second) but the build costs more."""
    if which("gs"):
        # The Info fields are merged too, the later file's winning; the
        # appendix writes none (see _APPENDIX_PREAMBLE) except pdfTeX's own
        # Creator, which the pdfmark puts back.
        ok = subprocess.run(
            ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
             *GS_LOSSLESS, f"-sOutputFile={out}", a, b,
             "-c", f"[ /Creator (latexdiff-zip {__version__}) /DOCINFO pdfmark"],
            stdout=DEVNULL, stderr=DEVNULL).returncode == 0
        if ok and os.path.isfile(out):
            return True
    if which("pdfunite"):
        return subprocess.run(["pdfunite", a, b, out],
                              stdout=DEVNULL, stderr=DEVNULL).returncode == 0
    return False


# ---- PDF build ------------------------------------------------------------

def _creator_hook():
    """TeX code read before diff.tex that names this tool, with its version, as
    the PDF's Creator. A document with hyperref gets it through \\hypersetup,
    since hyperref writes the Info dictionary itself: a hook set before
    \\documentclass runs after the packages' own. Anything else gets \\pdfinfo.
    The gs merge keeps the first file's Info, so the final PDF carries it."""
    creator = f"latexdiff-zip {__version__}"
    return (r"\AtBeginDocument{\ifdefined\hypersetup\hypersetup{pdfcreator={" + creator
            + r"}}\else\pdfinfo{/Creator (" + creator + r")}\fi}")


# Baseline of the page label, above the bottom edge of the page.
STAMP_BASELINE = 9.0               # bp


def stamp_hook():
    """TeX code that prints a small label at the bottom right of every page:
    "Made with latexdiff-zip vX.Y.Z · latexdiff.toftul.net ·
    github.com/toftul/latexdiff-zip", grey, in the document's own sans serif,
    both addresses links. A page too narrow for that (an 8 cm demo page)
    drops the GitHub address; the label measures itself to decide.

    It draws in LaTeX's shipout/foreground hook, so it needs no change to the
    latexdiff output and cannot move anything on the page. 9 bp up from the
    edge it sits under every class's own footer. Skipped on beamer slides,
    whose navigation symbols and footlines fill that corner, and on a LaTeX
    older than the hook (2020-10)."""
    def link(url):
        return rf"\ldzlink{{{url}}}{{{url.split('//', 1)[1].rstrip('/')}}}"
    sep = r"\enspace$\cdot$\enspace "
    short = f"Made with latexdiff-zip v{__version__}" + sep + link(SITE_URL)
    return (
        r"\ifdefined\AddToHook"
        r"\def\ldzpagewd{\ifdim\pdfpagewidth>0pt \pdfpagewidth\else\paperwidth\fi}"
        r"\def\ldzpageht{\ifdim\pdfpageheight>0pt \pdfpageheight\else\paperheight\fi}"
        r"\def\ldzlink#1#2{\pdfstartlink attr{/Border[0 0 0]}"
        r"user{/Subtype/Link/A<</Type/Action/S/URI/URI(#1)>>}#2\pdfendlink}"
        r"\def\ldzplain#1#2{#2}"
        r"\def\ldzshort{" + short + "}"
        r"\def\ldzfull{" + short + sep + link(REPO_URL) + "}"
        r"\def\ldzstamp{\ifdefined\usebeamertemplate\else{"
        r"\normalfont\sffamily\fontsize{6.5}{8}\selectfont"
        r"\ifdefined\color\color[gray]{0.42}\fi"
        r"\setbox0\hbox{\let\ldzlink\ldzplain\ldzfull}"
        r"\ifdim\wd0>\dimexpr\ldzpagewd-24bp\relax\let\ldzlabel\ldzshort"
        r"\else\let\ldzlabel\ldzfull\fi"
        r"\put(0,0){\smash{\raisebox{"
        rf"-\dimexpr\ldzpageht-{STAMP_BASELINE}bp\relax}}{{"
        r"\makebox[\ldzpagewd][r]{\ldzlabel\hspace{12bp}}}}}}\fi}"
        r"\AddToHook{shipout/foreground}{\ldzstamp}\fi"
    )


def runtex(build, label=True):
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-jobname=diff",
                    _creator_hook() + (stamp_hook() if label else "")
                    + r"\input{diff.tex}"],
                   cwd=build, stdout=DEVNULL, stderr=DEVNULL)


def build_diff_pdf(build, main_new, bib_inline=False, label=True):
    """The fixed pdflatex -> bib backend -> pdflatex x2 sequence, ignoring
    non-zero exits (latexdiff markup routinely trips minor errors; the classic
    sequence still converges where latexmk stops too early). `bib_inline` says
    the diff already holds the formatted bibliography (expanded .bbl), `label`
    adds the page label. Returns whether a diff.pdf came out."""
    for f in ("diff.aux", "diff.bbl", "diff.bcf", "diff.blg", "diff.pdf"):
        remove(os.path.join(build, f))

    runtex(build, label)                          # pass 1: write .aux

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
    # Never when the bibliography is inline: REVTeX still writes
    # \bibdata{diffNotes} for its bib notes, and at the end of the inline
    # thebibliography it reads diff.bbl, so an adopted .bbl prints a second,
    # continued reference list that the citations then point into.
    needs_bib = os.path.isfile(diff_bcf) or file_has(diff_aux, "\\bibdata")
    if needs_bib and not bib_inline \
            and not (file_has(diff_bbl, "\\bibitem") or file_has(diff_bbl, "\\entry")):
        shipped = os.path.join(build, os.path.splitext(os.path.basename(main_new))[0] + ".bbl")
        if os.path.isfile(shipped) and os.path.getsize(shipped) > 0:
            shutil.copy(shipped, diff_bbl)

    runtex(build, label)                          # pass 2: pull in .bbl, resolve labels
    runtex(build, label)                          # pass 3: read \bibcite back
    return os.path.isfile(os.path.join(build, "diff.pdf"))


def hide_deletions(diff_tex):
    """Turn a CFONT diff into the BLUE style by adding BLUE_PREAMBLE right
    before \\begin{document}. Works on bytes so a non-UTF-8 source survives."""
    with open(diff_tex, "rb") as f:
        data = f.read()
    m = _BEGIN_DOC_RE.search(data)
    if m is None:
        warn("warning: no \\begin{document} in the diff; deletions will show")
        return
    with open(diff_tex, "wb") as f:
        f.write(data[:m.start()] + BLUE_PREAMBLE + data[m.start():])


# ---- main -----------------------------------------------------------------

def main(argv):
    args = parse_args(argv)
    say(f"latexdiff-zip {__version__}")           # a pasted log says what made the PDF
    main_old = args["main_old"]
    main_new = args["main_new"]
    out_pdf = args["out_pdf"]
    diff_type = args["diff_type"]
    embed_figs = args["embed_figs"]
    page_label = args["page_label"]
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

        blue = diff_type == BLUE_TYPE
        ld_type = "CFONT" if blue else diff_type

        def run_latexdiff(old_f, new_f, extra):
            with open(diff_tex, "wb") as out, open(latexdiff_log, "ab") as errf:
                ld = subprocess.run(
                    ["latexdiff", f"--type={ld_type}", "--append-safecmd=label",
                     *extra, old_f, new_f], stdout=out, stderr=errf)
            if blue and diff_written():
                hide_deletions(diff_tex)
            return ld

        def diff_written():
            return os.path.isfile(diff_tex) and os.path.getsize(diff_tex) > 0

        say(f"running latexdiff (type={diff_type})...")
        gfx = [] if blue else [GRAPHICS_MARKUP]
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
        cur_old, cur_new, bib_inline = old_flat, new_flat, bbl_expanded
        ok = build_diff_pdf(build, main_new, bib_inline, page_label)
        # latexdiff markup inside a formatted bibliography occasionally produces
        # unbuildable TeX; redo the diff from the unexpanded flats (giving up
        # only the bibliography markup) rather than fail.
        if not ok and bbl_expanded:
            warn("warning: build failed with expanded bibliographies; retrying without them")
            cur_old, cur_new, bib_inline = old_flat_noexp, new_flat_noexp, False
            run_latexdiff(cur_old, cur_new, gfx)
            ok = build_diff_pdf(build, main_new, bib_inline, page_label)
        # Deleted-figure markup is the other thing that can make a document
        # unbuildable; shed that too rather than fail.
        if not ok and gfx:
            warn("warning: build failed with deleted-figure markup; retrying without it")
            run_latexdiff(cur_old, cur_new, [])
            build_diff_pdf(build, main_new, bib_inline, page_label)

        if not os.path.isfile(os.path.join(build, "diff.pdf")):
            warn("error: PDF build failed. Tail of diff.log:")
            tail_to_stderr(os.path.join(build, "diff.log"), 40)
            sys.exit(1)

        shutil.copy(os.path.join(build, "diff.pdf"), out_pdf)

        say("comparing figures...")
        labels = read_aux_labels(os.path.join(build, "diff.aux"),
                                 os.path.join(build, "diff.log"))
        figs = compare_figures(old_root, new_root, old_flat, new_flat, tmp, labels)

        if figs and (embed_figs or fig_dir):
            page = appendix_page_size(pdf_page_size(out_pdf))
            figapp = os.path.join(tmp, "figapp")
            figures_pdf, level = build_fig_appendix(figs, figapp, page, page_label)

            if fig_dir:
                n = export_fig_pngs(figs, figapp, page, level or "minimal", fig_dir)
                if n:
                    say(f"wrote {n} figure comparison PNG(s) to: {fig_dir}/")
                else:
                    warn("warning: could not write the figure comparison PNGs")

            if embed_figs:
                combined = os.path.join(tmp, "combined.pdf")
                if figures_pdf is None:
                    warn("warning: could not typeset the figure comparisons; the PDF has none")
                elif merge_pdfs(out_pdf, figures_pdf, combined):
                    shutil.copy(combined, out_pdf)
                    say(f"appended {len(figs)} figure comparison(s) to the PDF")
                else:
                    warn("warning: could not embed the figure comparisons (need gs or pdfunite to merge);")
                    warn("         re-run with -c DIR to save them as PNG files instead.")

        say(f"wrote: {out_pdf}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
