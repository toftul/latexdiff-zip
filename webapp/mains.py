"""List the plausible main .tex files inside a project archive.

Standalone and stdlib-only: it reads the archive's index and the small top-level
.tex members straight out of the stream, never unpacking anything to disk. Used
by the web UI to fill the "main .tex" dropdowns as soon as a file is chosen, so
an archive holding several \\documentclass files can be resolved with a click
instead of a failed build and a typed filename.

Deliberately not a call into latexdiff-zip.py: the engine's detect_main() takes
an already-extracted directory, returns exactly one name, and exits the process
when there are none or several -- while this needs an un-extracted archive and
treats "several" as the interesting answer.

The rule it applies is the engine's, though, and must stay no more permissive
than it: a file we suggest but the engine can't use is worse than no suggestion
at all. Top-level *.tex only, after descending a lone top-level directory, kept
when a line starts with \\documentclass.
"""

import contextlib
import re
import tarfile
import zipfile

# Largest .tex member we will read to look for \documentclass. A main file is
# never anywhere near this; the cap just keeps a hostile archive from making us
# pull a huge member into memory.
MAX_TEX_BYTES = 2 * 1024 * 1024

# Matched against raw bytes, so no decoding step can fail on an oddly encoded
# source file. Mirrors _DOCCLASS_RE in latexdiff-zip.py.
_DOCCLASS_RE = re.compile(rb"^[^\S\n]*\\documentclass", re.M)


def _seek0(src):
    """Rewind a file object between format probes; a path needs nothing."""
    if hasattr(src, "seek"):
        src.seek(0)


def _open_zip(src):
    _seek0(src)
    zf = zipfile.ZipFile(src)
    members = [(i.filename, i.file_size, i) for i in zf.infolist() if not i.is_dir()]
    return zf, members, lambda info: zf.read(info)[:MAX_TEX_BYTES]


def _open_tar(src):
    _seek0(src)
    kwargs = {"fileobj": src} if hasattr(src, "read") else {"name": src}
    tf = tarfile.open(mode="r:*", **kwargs)   # auto-detects gzip/bzip2/xz
    members = [(m.name, m.size, m) for m in tf.getmembers() if m.isfile()]

    def read(member):
        f = tf.extractfile(member)
        return f.read(MAX_TEX_BYTES) if f else b""

    return tf, members, read


def _open_archive(src):
    """Sniff the format and return (handle, [(name, size, member)], read_fn),
    or None if `src` is neither a zip nor a tar archive."""
    _seek0(src)
    if zipfile.is_zipfile(src):
        return _open_zip(src)
    _seek0(src)
    if tarfile.is_tarfile(src):
        return _open_tar(src)
    return None


def _strip_single_root(names):
    """Drop a lone top-level directory from every name, as the engine does.

    Mirrors descend_single_root() in latexdiff-zip.py: an archive whose entries
    all live under one folder is the same project as one packed flat."""
    if not all("/" in n for n in names):
        return names            # something sits at the top level already
    if len({n.split("/", 1)[0] for n in names}) != 1:
        return names            # more than one top-level folder: no descent
    return [n.split("/", 1)[1] for n in names]


def candidates(src):
    """Names of the top-level .tex files in `src` that contain \\documentclass.

    `src` is a path or an open binary file object. Returns a sorted list, empty
    if the archive is unreadable or holds no candidate -- this is a hint for the
    UI, never an error path."""
    try:
        opened = _open_archive(src)
        if opened is None:
            return []
        handle, members, read = opened
        with contextlib.closing(handle):
            names = _strip_single_root([name for name, _, _ in members])
            found = []
            for name, (_, size, member) in zip(names, members):
                if "/" in name or not name.lower().endswith(".tex"):
                    continue
                if size > MAX_TEX_BYTES:
                    continue
                if _DOCCLASS_RE.search(read(member)):
                    found.append(name)
            return sorted(found)
    except Exception:
        return []
