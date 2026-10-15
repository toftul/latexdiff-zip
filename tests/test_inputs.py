"""Unit tests for the engine's input planning and its missing-files scan.

What each OLD/NEW argument is (archive, .tex, folder, arXiv id), which files a
folder input brings along, and which files a build log says are missing. No
LaTeX and no network; it runs instantly:

    python3 tests/test_inputs.py
    python3 -m unittest tests.test_inputs

The end-to-end behaviour (a real build from old.tex and new.tex) is in
test_parity.py; this pins the parts that are hard to reach from there, such
as a file name that TeX wrapped across two lines of its log.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _engine():
    path = os.path.join(ROOT, "latexdiff-zip.py")
    spec = importlib.util.spec_from_file_location("ldz_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # import-only: the engine guards main()
    return mod


E = _engine()
DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def write(self, rel, text=""):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class InputKindTest(Tmp):
    def test_kinds(self):
        self.assertEqual(E.input_kind(self.dir), "dir")
        self.assertEqual(E.input_kind(self.write("paper.tex", DOC)), "tex")
        self.assertEqual(E.input_kind(self.write("PAPER.TEX", DOC)), "tex")
        self.assertEqual(E.input_kind(self.write("v1.tar.gz")), "archive")
        self.assertEqual(E.input_kind("2401.12345v1"), "arxiv")

    def test_rejects_other_files_and_strings(self):
        for arg in (self.write("notes.txt"), "gibberish"):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                E.input_kind(arg)


class FolderFilesTest(Tmp):
    def rels(self, src, keep=""):
        return sorted(rel for _full, rel in E.folder_files(src, "test", keep))

    def test_skips_dot_entries_but_keeps_a_named_main(self):
        self.write("main.tex", DOC)
        self.write("figs/plot.png")
        self.write(".DS_Store")
        self.write(".git/config")
        self.write(".hidden.tex", DOC)
        self.assertEqual(self.rels(self.dir), ["figs/plot.png", "main.tex"])
        self.assertEqual(self.rels(self.dir, keep=".hidden.tex"),
                         [".hidden.tex", "figs/plot.png", "main.tex"])

    def test_does_not_follow_directory_symlinks(self):
        self.write("main.tex", DOC)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        with open(os.path.join(outside.name, "huge.bin"), "w") as f:
            f.write("x")
        os.symlink(outside.name, os.path.join(self.dir, "linked"))
        self.assertEqual(self.rels(self.dir), ["main.tex"])

    def test_over_the_limit_exits(self):
        self.write("main.tex", DOC)
        with open(os.path.join(self.dir, "blob.bin"), "wb") as f:
            f.truncate(E.FOLDER_LIMIT_MB * 1000 * 1000 + 1)    # sparse
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            E.folder_files(self.dir, "folder x")
        self.assertIn("folder x holds more than", err.getvalue())


class SameProjectTest(unittest.TestCase):
    def test_folder_kinds_compare_by_folder(self):
        a = {"kind": "tex", "src": "/p"}
        self.assertTrue(E.same_project(a, {"kind": "dir", "src": "/p"}))
        self.assertTrue(E.same_project(a, {"kind": "tex", "src": "/p"}))
        self.assertFalse(E.same_project(a, {"kind": "tex", "src": "/q"}))
        self.assertFalse(E.same_project(a, {"kind": "arxiv", "src": "/p"}))

    def test_arxiv(self):
        a = {"kind": "arxiv", "src": "2401.12345v1"}
        self.assertTrue(E.same_project(a, dict(a)))
        self.assertFalse(E.same_project(a, {"kind": "arxiv", "src": "2401.12345v2"}))


class MissingFilesTest(Tmp):
    def test_log_and_blg(self):
        long = "fig_" + "x" * 90
        line = f"! LaTeX Error: File `{long}' not found."
        # TeX breaks .log lines at 79 bytes; the name must come out whole.
        wrapped = "\n".join(line[i:i + 79] for i in range(0, len(line), 79))
        self.write("diff.log", "\n".join([
            "This is pdfTeX",
            wrapped,
            "! Package pdftex.def Error: File `figs/plot.pdf' not found: using draft sett",
            "ing.",
            "! LaTeX Error: File `figs/plot.pdf' not found.",    # repeated: listed once
            "! LaTeX Error: File `revtex4-2.cls' not found.",
            "LaTeX Warning: Citation `x' undefined.",
        ]) + "\n")
        self.write("diff.blg", "I couldn't open database file refs.bib\n"
                               "I couldn't open style file mystyle.bst\n"
                               "[42] Utils.pm:411> ERROR - Cannot find 'more.bib'!\n")
        self.assertEqual(E.missing_files(self.dir),
                         [long, "figs/plot.pdf", "revtex4-2.cls",
                          "refs.bib", "mystyle.bst", "more.bib"])

    def test_utf8_name(self):
        self.write("diff.log", "! LaTeX Error: File `Abbildung_ä.png' not found.\n")
        self.assertEqual(E.missing_files(self.dir), ["Abbildung_ä.png"])

    def test_nothing_missing(self):
        self.assertEqual(E.missing_files(self.dir), [])       # no log at all
        self.write("diff.log", "Output written on diff.pdf (1 page).\n")
        self.assertEqual(E.missing_files(self.dir), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
