"""Unit tests for the figure appendix's pure helpers in latexdiff-zip.py.

No LaTeX, no ImageMagick: these pin the decisions the appendix page is built
from -- page size, side-by-side vs stacked, the side labels, escaping, the
document order of the comparisons -- and run in well under a second:

    python3 tests/test_figpage.py
    python3 -m unittest tests.test_figpage

test_parity.py covers the typeset result end to end.
"""

import importlib.util
import os
import shutil
import struct
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _engine():
    """Import latexdiff-zip.py (hyphens: needs importlib)."""
    path = os.path.join(ROOT, "latexdiff-zip.py")
    spec = importlib.util.spec_from_file_location("ldz_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # import-only: the engine guards main()
    return mod


E = _engine()

LETTER = (612.0, 792.0)


class PageSizeTest(unittest.TestCase):
    def test_document_sizes_are_kept(self):
        for size in (E.A4, LETTER, (E.A4[1], E.A4[0]),       # A4 landscape
                     (419.53, 595.28), (841.89, 1190.55)):   # A5, A3
            self.assertEqual(E.appendix_page_size(size), size)

    def test_odd_sizes_become_a4_in_the_papers_orientation(self):
        self.assertEqual(E.appendix_page_size((226.77, 283.46)), E.A4)   # 8x10 cm
        self.assertEqual(E.appendix_page_size((2383.94, 3370.39)), E.A4)  # A0
        beamer = E.appendix_page_size((364.19, 273.14))
        self.assertEqual(beamer, (E.A4[1], E.A4[0]))                    # landscape

    def test_unknown_size_is_a4(self):
        self.assertEqual(E.appendix_page_size(None), E.A4)


class PdfPageSizeTest(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.remove, path)
        return path

    def test_plain_mediabox(self):
        p = self._write(b"%PDF-1.4\n3 0 obj << /Type /Page /MediaBox [0 0 612 792] >>")
        self.assertEqual(E.pdf_page_size(p), (612.0, 792.0))

    def test_mediabox_inside_compressed_object_stream(self):
        # pdfTeX's default: page dictionaries live in a Flate object stream.
        objs = zlib.compress(b"3 0 << /Type /Page /MediaBox [0 0 226.77 283.46] >>")
        data = (b"%PDF-1.5\n5 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Length "
                + str(len(objs)).encode() + b" /Filter /FlateDecode >>\nstream\n"
                + objs + b"\nendstream\nendobj\n")
        self.assertEqual(E.pdf_page_size(self._write(data)), (226.77, 283.46))

    def test_no_mediabox(self):
        self.assertIsNone(E.pdf_page_size(self._write(b"not a pdf")))
        self.assertIsNone(E.pdf_page_size("/nonexistent/file.pdf"))


class PngSizeTest(unittest.TestCase):
    def test_reads_ihdr(self):
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                    + struct.pack(">II", 637, 837) + b"\x08\x02\x00\x00\x00")
        self.addCleanup(os.remove, path)
        self.assertEqual(E.png_size(path), (637, 837))

    def test_not_a_png(self):
        self.assertIsNone(E.png_size(__file__))


class LayoutTest(unittest.TestCase):
    # A4 text block with the appendix margins.
    L = E.A4[0] - 2 * E.APPENDIX_MARGIN
    H = E.A4[1] - 2 * E.APPENDIX_MARGIN

    def layout(self, *aspects):
        return E.choose_layout(list(aspects), self.L, self.H)

    def test_tall_and_square_figures_sit_side_by_side(self):
        self.assertEqual(self.layout(0.7, 0.76), "side")    # the Dirac portraits
        self.assertEqual(self.layout(1.0, 1.0), "side")     # a tie goes to side

    def test_wide_figures_stack(self):
        self.assertEqual(self.layout(1.6, 1.6), "stack")
        self.assertEqual(self.layout(1.0, 1.73), "stack")   # mixed: the cat and cow

    def test_one_sided_figure_uses_its_own_shape(self):
        self.assertEqual(self.layout(0.7), "side")
        self.assertEqual(self.layout(2.0), "stack")

    def test_fit_keeps_the_aspect_inside_the_box(self):
        w, h = E._fit(2.0, 100, 100)
        self.assertEqual((w, h), (100, 50))
        w, h = E._fit(0.5, 100, 100)
        self.assertEqual((w, h), (50, 100))


class SideLabelsTest(unittest.TestCase):
    def test_different_sources(self):
        self.assertEqual(E.side_labels("dirac-v1.zip", "dirac-v2.zip", "a.tex", "a.tex"),
                         ("dirac-v1.zip", "dirac-v2.zip"))

    def test_same_source_adds_the_main_tex(self):
        self.assertEqual(E.side_labels("both.zip", "both.zip", "old.tex", "new.tex"),
                         ("both.zip \u00b7 old.tex", "both.zip \u00b7 new.tex"))

    def test_nothing_tells_them_apart(self):
        self.assertEqual(E.side_labels("arXiv:2401.1", "arXiv:2401.1", "m.tex", "m.tex"),
                         (None, None))


class TexEscapeTest(unittest.TestCase):
    def test_specials(self):
        self.assertEqual(E.tex_escape("fig_1 & 50% #2 {x} ~^$\\"),
                         r"fig\_1 \& 50\% \#2 \{x\} \textasciitilde{}"
                         r"\textasciicircum{}\$\textbackslash{}")

    def test_typography_and_unsupported_scripts(self):
        self.assertEqual(E.tex_escape("\u201cR\u00e9sum\u00e9\u201d \u2014 ok"),
                         "``R\u00e9sum\u00e9'' --- ok")
        self.assertEqual(E.tex_escape("\u0421\u0442\u0430\u0442\u044c\u044f.zip"),
                         "??????.zip")


class MatchOrderTest(unittest.TestCase):
    """match_figures returns the comparisons in the paper's order."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ldz-figpage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def project(self, side, body, files):
        root = os.path.join(self.tmp, side)
        os.makedirs(root)
        for name, data in files.items():
            with open(os.path.join(root, name), "wb") as f:
                f.write(data)
        flat = os.path.join(self.tmp, side + "_flat.tex")
        with open(flat, "w") as f:
            f.write("\\begin{document}\n" + body + "\n\\end{document}\n")
        return root, flat

    @staticmethod
    def fig(name, label=""):
        lbl = f"\\label{{{label}}}" if label else ""
        return f"\\begin{{figure}}\\includegraphics{{{name}}}{lbl}\\end{{figure}}\n"

    def test_removed_figure_follows_its_old_predecessor(self):
        # OLD: a, x, b    NEW: a (edited), b (same), c (new)
        old_root, old_flat = self.project(
            "old", self.fig("a", "fig:a") + self.fig("x", "fig:x") + self.fig("b", "fig:b"),
            {"a.png": b"a1", "x.png": b"x", "b.png": b"b"})
        new_root, new_flat = self.project(
            "new", self.fig("a", "fig:a") + self.fig("b", "fig:b") + self.fig("c", "fig:c"),
            {"a.png": b"a2", "b.png": b"b", "c.png": b"c"})
        entries = E.match_figures(old_root, new_root, old_flat, new_flat)
        self.assertEqual([(e["status"], e["name"]) for e in entries],
                         [("changed", "fig_a"), ("removed", "fig_x"), ("added", "fig_c")])

    def test_removed_figure_before_any_kept_one_comes_first(self):
        old_root, old_flat = self.project(
            "old", self.fig("x", "fig:x") + self.fig("a", "fig:a"),
            {"x.png": b"x", "a.png": b"a1"})
        new_root, new_flat = self.project(
            "new", self.fig("a", "fig:a"), {"a.png": b"a2"})
        entries = E.match_figures(old_root, new_root, old_flat, new_flat)
        self.assertEqual([e["name"] for e in entries], ["fig_x", "fig_a"])


class AppendixTexTest(unittest.TestCase):
    """The generated LaTeX, without compiling it."""

    def entries(self):
        ref = {"label": "fig:a_b", "path": "figs/a_b", "pos": 0}
        return [
            {"status": "changed", "name": "fig_a_b", "old_ref": ref, "new_ref": ref,
             "old": "o.png", "new": "n.png", "old_aspect": 1.0, "new_aspect": 1.0},
            {"status": "added", "name": "fig_c", "old_ref": None,
             "new_ref": {"label": "", "path": "c", "pos": 9},
             "old": None, "new": "c.png", "old_aspect": None, "new_aspect": 2.0},
        ]

    def tex(self, level, **kw):
        staged = [{"old": "f000o.png", "new": "f000n.png"},
                  {"old": None, "new": "f001n.png"}]
        return E.appendix_tex(self.entries(), staged, E.A4, ("v1.zip", "v2.zip"),
                              level, **kw)

    def test_every_level_has_the_blocks_and_labels(self):
        for level in E.APPENDIX_LEVELS:
            tex = self.tex(level)
            self.assertEqual(tex.count(r"\begin{ldzblock}"), 2, level)
            self.assertEqual(tex.count(r"\end{ldzblock}"), 2, level)
            self.assertIn(r"\ldzchanged", tex)
            self.assertIn(r"\ldzadded", tex)
            self.assertIn("Not in OLD version", tex)
            self.assertIn(r"fig:a\_b", tex)                 # escaped title
            self.assertIn("v1.zip", tex)

    def test_minimal_level_needs_no_extra_packages(self):
        tex = self.tex("minimal")
        for pkg in ("tikz", "tgheros", "xcolor", "fontenc"):
            self.assertNotIn(pkg, tex)

    def test_single_mode_puts_each_block_on_its_own_page(self):
        tex = self.tex("full", single=True)
        self.assertEqual(tex.count(r"\clearpage"), 2)
        self.assertNotIn("Figure changes}", tex)            # no page title

    def test_unique_names(self):
        es = [{"name": "fig_p"}, {"name": "fig_p"}, {"name": "x"}]
        self.assertEqual(E._unique_names(es), ["fig_p", "fig_p-2", "x"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
