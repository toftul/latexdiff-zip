"""The version number: one value, everywhere it is shown.

The engine's __version__ is the only place to bump. These tests tie the rest to
it: the -V flag, the first log line, the newest release in CHANGELOG.md, the web
page footer, and a release tag on the current commit (tag-release.sh makes that
tag from __version__). No LaTeX, no Flask; instant:

    python3 tests/test_version.py
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "latexdiff-zip.py")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_webjob import app as webapp            # noqa: E402  (flask stubbed)


def _engine():
    spec = importlib.util.spec_from_file_location("ldz_engine", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # import-only: the engine guards main()
    return mod


VERSION = _engine().__version__


def run(*args):
    return subprocess.run([sys.executable, ENGINE, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


class VersionTest(unittest.TestCase):
    def test_is_semver(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_flag(self):
        for flag in ("-V", "--version"):
            p = run(flag)
            self.assertEqual(p.returncode, 0, flag)
            self.assertEqual(p.stdout.strip(), f"latexdiff-zip {VERSION}", flag)

    def test_first_log_line(self):
        # Two inputs that are neither files nor arXiv ids: the run stops before
        # any LaTeX, but after logging the version.
        p = run("no-such-old.zip", "no-such-new.zip")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.splitlines()[0], f"latexdiff-zip {VERSION}")

    def test_changelog_has_this_release_on_top(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
        releases = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", text, re.M)
        self.assertTrue(releases, "no dated release heading in CHANGELOG.md")
        self.assertEqual(releases[0], VERSION,
                         "bump __version__ and CHANGELOG.md together")
        self.assertIn("## [Unreleased]", text)
        for v in releases:
            self.assertRegex(text, rf"(?m)^\[{re.escape(v)}\]: https://", f"link for {v}")

    def test_web_page_shows_the_engines_version(self):
        self.assertEqual(webapp.ENGINE_VERSION, VERSION)

    def test_release_tag_on_this_commit_matches(self):
        # A tag lives in git, not in a file, so it can drift from __version__.
        # Any vX.Y.Z tag on the commit under test must be this version's.
        if not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        p = subprocess.run(["git", "tag", "--points-at", "HEAD", "--list", "v[0-9]*"],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True)
        for tag in p.stdout.split():
            self.assertEqual(tag, f"v{VERSION}", "tag and __version__ disagree")


if __name__ == "__main__":
    unittest.main(verbosity=2)
