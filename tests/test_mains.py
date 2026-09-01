"""Tests for webapp/mains.py -- the archive scan behind the web UI's main-.tex
dropdowns.

Needs neither Flask nor a LaTeX toolchain, so it runs in a second:

    python3 tests/test_mains.py
    python3 -m unittest tests.test_mains

The last test is the one that matters most. mains.candidates() deliberately
does not call into the engine (it reads an un-extracted archive and must return
*all* candidates, while detect_main() takes a directory, returns one, and exits
on ambiguity), so the two implementations of "which .tex is a main file" could
drift apart. test_agrees_with_engine pins them together over every fixture in
tests/cases/.
"""

import importlib.util
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(ROOT, "tests", "cases")
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import mains                                      # noqa: E402
from test_parity import zip_tree                  # noqa: E402


def _engine():
    """Import latexdiff-zip.py for its detect_main (hyphens: needs importlib)."""
    path = os.path.join(ROOT, "latexdiff-zip.py")
    spec = importlib.util.spec_from_file_location("ldz_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # import-only: the engine guards main()
    return mod


def make_zip(path, files):
    """Write a zip from a {name: text} mapping."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"


class TestCandidates(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.work, name)

    # ---- real repo fixtures ------------------------------------------------

    def test_sample_archives(self):
        """The repo's own smoke-test zips, packed flat."""
        self.assertEqual(mains.candidates(os.path.join(ROOT, "test_for_diff_old.zip")),
                         ["main.tex"])
        self.assertEqual(mains.candidates(os.path.join(ROOT, "test_for_diff_new.zip")),
                         ["test.tex"])

    def test_ambiguous_archive_lists_all(self):
        """The case the feature exists for: one archive, two mains."""
        zip_tree(os.path.join(CASES_DIR, "same-archive", "both"), self.path("both.zip"))
        self.assertEqual(mains.candidates(self.path("both.zip")),
                         ["new.tex", "old.tex"])

    # ---- archive shapes ----------------------------------------------------

    def test_accepts_a_file_object(self):
        """Werkzeug hands the route an open stream, not a path."""
        with open(os.path.join(ROOT, "test_for_diff_old.zip"), "rb") as f:
            self.assertEqual(mains.candidates(f), ["main.tex"])

    def test_single_top_level_dir_is_descended(self):
        """An archive packed under one folder is the same project as a flat one."""
        make_zip(self.path("nested.zip"),
                 {"project/main.tex": DOC, "project/figs/a.tex": DOC})
        self.assertEqual(mains.candidates(self.path("nested.zip")), ["main.tex"])

    def test_two_top_level_dirs_are_not_descended(self):
        """Matching the engine: with no single root there is nothing at the top."""
        make_zip(self.path("two.zip"), {"a/main.tex": DOC, "b/main.tex": DOC})
        self.assertEqual(mains.candidates(self.path("two.zip")), [])

    def test_subdirectory_tex_is_ignored(self):
        """Only top-level .tex counts -- the engine globs root/*.tex."""
        make_zip(self.path("sub.zip"), {"main.tex": DOC, "chapters/intro.tex": DOC})
        self.assertEqual(mains.candidates(self.path("sub.zip")), ["main.tex"])

    def test_tex_without_documentclass_is_ignored(self):
        make_zip(self.path("plain.zip"),
                 {"main.tex": DOC, "notes.tex": "just a fragment\n"})
        self.assertEqual(mains.candidates(self.path("plain.zip")), ["main.tex"])

    def test_commented_out_documentclass_is_ignored(self):
        """\\documentclass must start the line, as _DOCCLASS_RE requires."""
        make_zip(self.path("cmt.zip"),
                 {"notes.tex": "% \\documentclass{article}\n"})
        self.assertEqual(mains.candidates(self.path("cmt.zip")), [])

    def test_tarball(self):
        """tar.gz reaches the same answer as the equivalent zip."""
        src = os.path.join(CASES_DIR, "article-nobib", "new")
        zip_tree(src, self.path("case.zip"))
        with tarfile.open(self.path("case.tar.gz"), "w:gz") as tf:
            for dirpath, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(dirpath, f)
                    tf.add(full, arcname=os.path.relpath(full, src))
        self.assertEqual(mains.candidates(self.path("case.tar.gz")),
                         mains.candidates(self.path("case.zip")))
        self.assertEqual(len(mains.candidates(self.path("case.tar.gz"))), 1)

    # ---- degrades quietly --------------------------------------------------

    def test_not_an_archive(self):
        self.assertEqual(mains.candidates(os.path.join(ROOT, "README.md")), [])

    def test_truncated_archive(self):
        with open(os.path.join(ROOT, "test_for_diff_old.zip"), "rb") as f:
            data = f.read()
        with open(self.path("broken.zip"), "wb") as f:
            f.write(data[:len(data) // 3])
        self.assertEqual(mains.candidates(self.path("broken.zip")), [])

    def test_missing_file(self):
        self.assertEqual(mains.candidates(self.path("nope.zip")), [])

    def test_empty_archive(self):
        make_zip(self.path("empty.zip"), {})
        self.assertEqual(mains.candidates(self.path("empty.zip")), [])

    # ---- drift guard -------------------------------------------------------

    def test_agrees_with_engine(self):
        """For every fixture project, the scan and detect_main() must agree.

        One candidate  -> detect_main returns that same name.
        Zero or several -> detect_main refuses (it exits), which is exactly when
        the UI needs to offer a choice."""
        detect_main = _engine().detect_main
        seen = 0
        for case in sorted(os.listdir(CASES_DIR)):
            for side in ("old", "new", "both"):
                src = os.path.join(CASES_DIR, case, side)
                if not os.path.isdir(src):
                    continue
                seen += 1
                z = self.path(f"{case}-{side}.zip")
                zip_tree(src, z)
                found = mains.candidates(z)
                with redirect_stderr(io.StringIO()):     # detect_main is chatty
                    if len(found) == 1:
                        self.assertEqual(detect_main(src, "-m"), found[0],
                                         f"{case}/{side}")
                    else:
                        with self.assertRaises(SystemExit, msg=f"{case}/{side}"):
                            detect_main(src, "-m")
        self.assertGreater(seen, 5, "fixtures went missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
