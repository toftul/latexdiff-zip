#!/usr/bin/env python3
"""Behaviour oracle for the latexdiff-zip engine.

Runs the engine under test over a matrix of committed fixture projects and
asserts on the externally observable contract: exit code, the stage / warning
log lines the web UI and users depend on, whether a PDF was produced, its page
count, and probe strings in its extracted text. This is the *executable*
definition of "behaves the same" for the planned Python rewrite -- point the
suite at the new engine and it must stay green.

    python3 tests/test_parity.py              # run against ./latexdiff-zip.py
    python3 -m unittest tests.test_parity     # same, via unittest discovery
    LDZ_SCRIPT=./some-other-engine tests/test_parity.py         # any engine build
    LDZ_NETWORK=1 python3 tests/test_parity.py                  # + arXiv cases
    python3 tests/test_parity.py -k fast      # only the fast CLI-contract cases

Fixtures live as plain source trees under tests/cases/<name>/{old,new}/ and are
zipped into a temp archive at run time (via stdlib zipfile -- no `zip` binary
needed), so the same suite exercises the extract path of either engine. The
full cases each drive a real pdflatex build and take ~10-30s; the `fast` cases
are pure CLI-contract checks with no LaTeX run.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(ROOT, "tests", "cases")

# Engine under test. Defaults to the Python engine; set LDZ_SCRIPT to the bash
# script to confirm the two still agree. A .py target is run through the current
# interpreter so it needs no execute bit. Absolutised so it resolves regardless
# of a case's temp working directory.
SCRIPT = os.path.abspath(os.environ.get("LDZ_SCRIPT", os.path.join(ROOT, "latexdiff-zip.py")))
RUN_NETWORK = os.environ.get("LDZ_NETWORK") == "1"
CASE_TIMEOUT = int(os.environ.get("LDZ_CASE_TIMEOUT", "300"))

HAVE_PDFINFO = shutil.which("pdfinfo") is not None
HAVE_PDFTOTEXT = shutil.which("pdftotext") is not None
HAVE_PDFIMAGES = shutil.which("pdfimages") is not None


def script_argv(args):
    base = [sys.executable, SCRIPT] if SCRIPT.endswith(".py") else [SCRIPT]
    return base + list(args)


def zip_tree(src_dir, dst_zip):
    """Zip the *contents* of src_dir (files at the archive root, recursing into
    subdirs) into dst_zip."""
    with zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(src_dir):
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                arc = os.path.relpath(full, src_dir)
                zf.write(full, arc)


def run(argv, cwd):
    proc = subprocess.run(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=CASE_TIMEOUT,
    )
    return proc.returncode, proc.stdout


def pdf_pages(path):
    out = subprocess.run(["pdfinfo", path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    return int(m.group(1)) if m else None


def pdf_images(path):
    """How many images the PDF embeds. The only way to tell from the outside
    that a *deleted* figure was drawn: its file has to have been found at
    compile time, and pdflatex reports a missing one only in its log."""
    out = subprocess.run(["pdfimages", "-list", path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    return sum(1 for line in out.splitlines() if re.match(r"\s*\d+\s+\d+\s+\w", line))


def pdf_text(path):
    return subprocess.run(["pdftotext", path, "-"], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True).stdout


class Case:
    """One row of the oracle. A fixture case zips old_dir/new_dir and diffs
    them; a `same_archive` case zips `both_dir` once and passes it twice; a
    `literal` case passes raw argv (for CLI-contract error checks)."""

    def __init__(self, name, *, old_dir=None, new_dir=None, both_dir=None,
                 same_archive=False, literal=None, unsupported_file=False,
                 args=(), exit=0, need_lines=(), forbid_lines=(),
                 want_pdf=True, min_pages=1, min_images=0,
                 probe_present=(), probe_absent=(), fast=False, network=False):
        self.name = name
        self.old_dir = old_dir
        self.new_dir = new_dir
        self.both_dir = both_dir
        self.same_archive = same_archive
        self.literal = literal
        self.unsupported_file = unsupported_file
        self.args = list(args)
        self.exit = exit
        self.need_lines = need_lines
        self.forbid_lines = forbid_lines
        self.want_pdf = want_pdf
        self.min_pages = min_pages
        self.min_images = min_images
        self.probe_present = probe_present
        self.probe_absent = probe_absent
        self.fast = fast
        self.network = network

    def build_inputs(self, work):
        """Return the two positional input args (paths / ids) for this case,
        materialising any temp archives under `work`."""
        if self.literal is not None:
            if self.unsupported_file:
                p = os.path.join(work, "notanarchive.txt")
                with open(p, "w") as f:
                    f.write("hello, not an archive\n")
                return [x if x != "<UNSUPPORTED>" else p for x in self.literal]
            return list(self.literal)
        if self.same_archive:
            z = os.path.join(work, "both.zip")
            zip_tree(os.path.join(CASES_DIR, self.both_dir), z)
            return [z, z]
        old_z = os.path.join(work, "old.zip")
        new_z = os.path.join(work, "new.zip")
        zip_tree(os.path.join(CASES_DIR, self.old_dir), old_z)
        zip_tree(os.path.join(CASES_DIR, self.new_dir), new_z)
        return [old_z, new_z]


CASES = [
    # ---- fast CLI-contract cases (no LaTeX run) ----------------------------
    Case("cli_no_args", literal=[], args=(), exit=2,
         need_lines=["Usage:"], want_pdf=False, fast=True),
    Case("cli_one_arg", literal=["only-one"], exit=2,
         need_lines=["Usage:"], want_pdf=False, fast=True),
    Case("cli_bad_input", literal=["gibberish", "also-gibberish"], exit=1,
         need_lines=["not a file or arXiv id"], want_pdf=False, fast=True),
    Case("cli_unsupported_type", literal=["<UNSUPPORTED>", "<UNSUPPORTED>"],
         unsupported_file=True, exit=1,
         need_lines=["unsupported archive type"], want_pdf=False, fast=True),

    # ---- full pipeline cases ----------------------------------------------
    Case("article_nobib", old_dir="article-nobib/old", new_dir="article-nobib/new",
         need_lines=["running latexdiff", "building PDF", "comparing figures"],
         forbid_lines=["expanding bibliographies", "error:"],
         min_pages=1),

    Case("revtex_arxiv_bbl_nobib",
         old_dir="revtex-arxiv-bbl-nobib/old", new_dir="revtex-arxiv-bbl-nobib/new",
         need_lines=["expanding bibliographies", "comparing figures"],
         probe_present=["Papakostas"], probe_absent=["??"], min_pages=5),

    # "shiny" (not "shiny new") because latexdiff splits inserted words across
    # \DIFadd wrappers, so pdftotext renders them non-contiguously -- probing a
    # single inserted word is what faithfully locks the current behaviour.
    Case("bibtex_regen", old_dir="bibtex-regen/old", new_dir="bibtex-regen/new",
         need_lines=["expanding bibliographies"],
         probe_present=["shiny", "Newcomer"], probe_absent=["??"], min_pages=1),

    Case("natbib", old_dir="natbib/old", new_dir="natbib/new",
         need_lines=["expanding bibliographies"],
         probe_absent=["??"], min_pages=1),

    Case("bib_missing", old_dir="bib-missing/old", new_dir="bib-missing/new",
         need_lines=["expanding bibliographies", "could not generate a .bbl"],
         min_pages=1),

    Case("biblatex", old_dir="biblatex/old", new_dir="biblatex/new",
         need_lines=["biblatex document"],
         forbid_lines=["expanding bibliographies"], min_pages=1),

    Case("same_archive", both_dir="same-archive/both", same_archive=True,
         args=["-M", "new.tex"], probe_present=["leaps"], min_pages=1),

    # ---- figure-diff features ---------------------------------------------
    # \graphicspath resolution: a bare \includegraphics{plot1} (found via
    # figs/) plus an explicit figs/plot2 -- both must resolve, so "2 changed".
    Case("figure_graphicspath",
         old_dir="figure-graphicspath/old", new_dir="figure-graphicspath/new",
         need_lines=["figure changed", "figure diff: 2 changed"], min_pages=1),

    # Byte-different but pixel-identical figure (a regenerated export): dropped,
    # so the diff reports no figure change.
    Case("figure_visually_identical",
         old_dir="figure-visually-identical/old", new_dir="figure-visually-identical/new",
         need_lines=["no figure changes detected"],
         forbid_lines=["figure changed", "figure added", "figure removed"],
         min_pages=1),

    # A figure present only in the new version gets a NEW ONLY page.
    Case("figure_added", old_dir="figure-added/old", new_dir="figure-added/new",
         need_lines=["figure added", "figure diff: 1 added"],
         forbid_lines=["figure changed", "figure removed"], min_pages=2),

    # A figure present only in the old version gets an OLD ONLY page.
    Case("figure_removed", old_dir="figure-removed/old", new_dir="figure-removed/new",
         need_lines=["figure removed", "figure diff: 1 removed"],
         forbid_lines=["figure changed", "figure added"], min_pages=2),

    # A figure only the old version had must still be *drawn* (crossed out at
    # reduced scale), not silently dropped: latexdiff keeps the deleted
    # \includegraphics live and the engine copies the old file in for it.
    # -F keeps the appendix out, so the image count is the body's alone: the
    # surviving figure plus the deleted one.
    Case("figure_deleted_renders",
         old_dir="figure-removed/old", new_dir="figure-removed/new",
         args=["-F"], need_lines=["old-only figures: 1 copied"],
         min_pages=1, min_images=2),

    # The same, for a renamed figure under \graphicspath{{figs/}}: the old file
    # is filed under the bare name the deleted command asks for, which LaTeX
    # finds in the document directory even with a graphics path set.
    Case("figure_renamed_graphicspath",
         old_dir="figure-renamed/old", new_dir="figure-renamed/new",
         args=["-F"], need_lines=["old-only figures: 1 copied"],
         min_pages=1, min_images=2),

    # Three \includegraphics under a single \label: panels must pair
    # one-to-one, so only the edited middle panel is reported. Pairing every
    # new panel with the first old ref instead gave 2 bogus changes + 2 bogus
    # removals.
    Case("figure_multipanel",
         old_dir="figure-multipanel/old", new_dir="figure-multipanel/new",
         need_lines=["figure diff: 1 changed"],
         forbid_lines=["figure added", "figure removed"], min_pages=2),

    # ---- network cases (opt-in: LDZ_NETWORK=1) ----------------------------
    Case("arxiv_natbib", literal=["1706.03762v1", "1706.03762v2"], args=["-F"],
         need_lines=["fetching arXiv", "running latexdiff"],
         probe_absent=["??"], min_pages=10, network=True),
    Case("arxiv_oldstyle_selfdiff",
         literal=["arXiv:math/0211159", "https://arxiv.org/abs/math/0211159"],
         need_lines=["fetching arXiv"], min_pages=1, network=True),
]


class ParityTest(unittest.TestCase):
    pass


def _make_test(case):
    def test(self):
        if case.network and not RUN_NETWORK:
            self.skipTest("network case; set LDZ_NETWORK=1 to run")
        work = tempfile.mkdtemp(prefix="ldz-parity-")
        try:
            inputs = case.build_inputs(work)
            out_pdf = os.path.join(work, "diff.pdf")
            argv = script_argv(case.args + ["-o", out_pdf] + inputs)
            code, output = run(argv, cwd=work)

            ctx = f"\n--- {case.name} exit={code}, argv={argv}\n{output}"
            self.assertEqual(code, case.exit, f"exit code{ctx}")
            for needle in case.need_lines:
                self.assertIn(needle, output, f"missing line {needle!r}{ctx}")
            for needle in case.forbid_lines:
                self.assertNotIn(needle, output, f"forbidden line {needle!r}{ctx}")

            if case.want_pdf:
                self.assertTrue(os.path.exists(out_pdf), f"no PDF produced{ctx}")
                if HAVE_PDFINFO and case.min_pages:
                    pages = pdf_pages(out_pdf)
                    self.assertIsNotNone(pages, f"pdfinfo gave no page count{ctx}")
                    self.assertGreaterEqual(
                        pages, case.min_pages,
                        f"only {pages} pages, want >= {case.min_pages}{ctx}")
                if HAVE_PDFIMAGES and case.min_images:
                    images = pdf_images(out_pdf)
                    self.assertGreaterEqual(
                        images, case.min_images,
                        f"only {images} image(s) embedded, want >= "
                        f"{case.min_images}{ctx}")
                if HAVE_PDFTOTEXT and (case.probe_present or case.probe_absent):
                    text = pdf_text(out_pdf)
                    for needle in case.probe_present:
                        self.assertIn(needle, text,
                                      f"probe {needle!r} absent from PDF{ctx}")
                    for needle in case.probe_absent:
                        self.assertNotIn(needle, text,
                                         f"probe {needle!r} present in PDF{ctx}")
            else:
                self.assertFalse(os.path.exists(out_pdf),
                                 f"unexpected PDF produced{ctx}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return test


for _c in CASES:
    # Put "fast" in the method name of the CLI-contract cases so the documented
    # `-k fast` filter selects exactly them.
    _prefix = "test_fast_" if _c.fast else "test_"
    setattr(ParityTest, f"{_prefix}{_c.name}", _make_test(_c))


if __name__ == "__main__":
    print(f"engine under test: {SCRIPT}", file=sys.stderr)
    unittest.main(verbosity=2)
