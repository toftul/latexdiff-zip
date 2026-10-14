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
             "old": "o.png", "new": "n.png", "old_aspect": 1.0, "new_aspect": 1.0,
             "number": None, "page": None, "caption": ""},
            {"status": "added", "name": "fig_c", "old_ref": None,
             "new_ref": {"label": "", "path": "c", "pos": 9},
             "old": None, "new": "c.png", "old_aspect": None, "new_aspect": 2.0,
             "number": "2", "page": "4", "caption": "An $a_b$ plot."},
        ]

    def tex(self, level, **kw):
        staged = [{"old": "f000o.png", "new": "f000n.png"},
                  {"old": None, "new": "f001n.png"}]
        return E.appendix_tex(self.entries(), staged, E.A4, level, **kw)

    def test_every_level_has_the_blocks_and_labels(self):
        for level in E.APPENDIX_LEVELS:
            tex = self.tex(level)
            self.assertEqual(tex.count(r"\begin{ldzblock}"), 2, level)
            self.assertEqual(tex.count(r"\end{ldzblock}"), 2, level)
            self.assertIn(r"\ldzchanged", tex)
            self.assertIn(r"\ldzadded", tex)
            self.assertIn("Not in OLD version", tex)
            self.assertIn(r"fig:a\_b", tex)                 # escaped title
            self.assertIn(r"\ldzside{ldzold}{OLD}", tex)     # the label, and nothing after it
            self.assertIn(r"\ldzside{ldznew}{NEW}" + "\\par", tex)

    def test_minimal_level_needs_no_extra_packages(self):
        tex = self.tex("minimal")
        for pkg in ("tikz", "tgheros", "xcolor", "fontenc"):
            self.assertNotIn(pkg, tex)

    def test_single_mode_puts_each_block_on_its_own_page(self):
        tex = self.tex("full", single=True)
        self.assertEqual(tex.count(r"\clearpage"), 2)
        self.assertNotIn("Figure changes}", tex)            # no page title

    def test_overview_links_every_comparison(self):
        tex = self.tex("full")
        self.assertIn(r"\pdfbookmark[0]{Figure changes}", tex)
        for i in (0, 1):
            self.assertIn(rf"\hyperlink{{ldz{i}}}", tex)
            self.assertIn(rf"\pageref{{ldz{i}}}", tex)
            self.assertIn(rf"\hypertarget{{ldz{i}}}", tex)
        self.assertIn(r"p.~4 in the diff", tex)
        self.assertIn(r"Figure 2", tex)
        self.assertIn(r"\ldzcaption{An \ldzmath{\$a\_b\$} plot.}", tex)

    def test_minimal_level_stubs_the_hyperref_commands(self):
        tex = self.tex("minimal")
        self.assertNotIn("hyperref", tex)
        self.assertIn(r"\providecommand{\hyperlink}", tex)
        self.assertIn(r"\providecommand{\pdfbookmark}", tex)


class NamesTest(unittest.TestCase):
    @staticmethod
    def entry(name, panel=(1, 1)):
        ref = {"label": "", "path": name, "pos": 0, "panel": panel}
        return {"status": "changed", "name": name, "old_ref": ref, "new_ref": ref}

    def test_unique_names(self):
        es = [self.entry("fig_p", (1, 3)), self.entry("fig_p", (3, 3)),
              self.entry("x"), self.entry("x")]
        self.assertEqual(E._unique_names(es), ["fig_p-p1", "fig_p-p3", "x", "x-2"])

    def test_ident(self):
        ref = {"label": "fig:a", "path": "figs/a", "panel": (2, 3)}
        e = {"status": "changed", "old_ref": ref, "new_ref": ref, "number": "4"}
        self.assertEqual(E.entry_ident(e), "Figure 4 \u00b7 panel 2 of 3")
        e["number"] = None
        ref["panel"] = (1, 1)
        self.assertEqual(E.entry_ident(e), "fig:a")
        ref["label"] = ""
        self.assertEqual(E.entry_ident(e), "figs/a")


class CaptionTest(unittest.TestCase):
    def test_commands_dropped_maths_kept(self):
        segs = E.caption_segments(
            r"Transmission of \textbf{gold} films, $T(\lambda)$ vs.~$d$ "
            r"(see Ref.~\cite{x}, Fig.~\ref{f})\label{fig:a}")
        self.assertEqual(segs, [
            ("text", "Transmission of gold films, "), ("math", r"$T(\lambda)$"),
            ("text", " vs. "), ("math", "$d$"),
            ("text", " (see Ref. [\u2026], Fig. \u2026)")])

    def test_accents_and_escapes(self):
        self.assertEqual(E.caption_segments(r'Schr\"odinger, Erd\H{o}s~--- 50\% \& more'),
                         [("text", "Schr\u00f6dinger, Erd\u0151s --- 50% & more")])

    def test_cut_at_a_word_with_an_ellipsis(self):
        segs = E.caption_segments("word " * 50, limit=22)
        self.assertEqual(segs, [("text", "word word word word\u2026")])

    def test_default_cut_is_100_characters(self):
        segs = E.caption_segments("abcd " * 40)
        text = "".join(t for _, t in segs)
        self.assertTrue(text.endswith("\u2026"))
        self.assertLessEqual(len(text), 101)
        self.assertGreater(len(text), 90)

    def test_maths_is_never_cut(self):
        segs = E.caption_segments("ab " + "$x+y+z$", limit=5)
        self.assertEqual(segs, [("text", "ab\u2026")])

    def test_empty(self):
        self.assertEqual(E.caption_segments(""), [])
        self.assertEqual(E.caption_segments(r"\label{x}"), [])


class AuxLabelsTest(unittest.TestCase):
    def labels(self, aux, log=""):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in (("diff.aux", aux), ("diff.log", log)):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        return E.read_aux_labels(os.path.join(d, "diff.aux"), os.path.join(d, "diff.log"))

    def test_plain_and_hyperref_forms(self):
        got = self.labels("\\newlabel{fig:a}{{1}{2}}\n"
                          "\\newlabel{fig:b}{{3}{iv}{Beta}{figure.3}{}}\n"
                          "\\newlabel{fig:c}{{}{5}{}{}{}}\n")
        self.assertEqual(got, {"fig:a": ("1", "2"), "fig:b": ("3", "iv"),
                               "fig:c": ("", "5")})

    def test_untrustworthy_labels_are_dropped(self):
        got = self.labels("\\newlabel{fig:c}{{2}{1}}\n\\newlabel{fig:c}{{3}{1}}\n"
                          "\\newlabel{fig:m}{{\\relax 2}{1}}\n",
                          "LaTeX Warning: Label `fig:c' multiply defined.\n")
        self.assertEqual(got, {})

    def test_annotate(self):
        def entry(status, numbered):
            ref = {"label": "fig:a", "path": "a", "caption": "Cap.", "numbered": numbered}
            return {"status": status, "old_ref": ref, "new_ref": ref}
        labels = {"fig:a": ("2", "7")}
        es = [entry("changed", True), entry("changed", False), entry("removed", True)]
        E.annotate_figures(es, labels)
        self.assertEqual([(e["number"], e["page"]) for e in es],
                         [("2", "7"), (None, "7"), (None, "7")])
        self.assertEqual(es[0]["caption"], "Cap.")


class ParseRefsTest(unittest.TestCase):
    def test_caption_numbering_and_panels(self):
        refs = E._parse_refs(
            "\\begin{figure}\\includegraphics{a}\\includegraphics{b}"
            "\\caption{Two \\emph{panels}.}\\label{fig:ab}\\end{figure}\n"
            "\\begin{figure}\\label{fig:c}\\includegraphics{c}\\caption{Label first.}"
            "\\end{figure}\n")
        self.assertEqual([(r["path"], r["panel"], r["numbered"], r["caption"]) for r in refs], [
            ("a", (1, 2), True, r"Two \emph{panels}."),
            ("b", (2, 2), True, r"Two \emph{panels}."),
            ("c", (1, 1), False, "Label first."),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
