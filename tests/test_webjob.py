"""Tests for the web backend's job bookkeeping (webapp/app.py).

Focused on one guarantee: every build leaves a status behind. The browser
watches /jobs/<id>/events, whose stream ends only when the status file reads
DONE or FAIL -- so a build thread that dies without writing one leaves the page
spinning forever, with no error and no way to tell it failed.

Flask is not needed (and is not installed outside the container), so app.py is
imported with a stub in its place; nothing here touches the routes:

    python3 tests/test_webjob.py
    python3 -m unittest tests.test_webjob
"""

import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))


def _load_app():
    """Import webapp/app.py with a stand-in for flask.

    app.py builds an app object and registers routes at import time. The stub
    only has to survive that: a Flask() with a .config mapping and route
    decorators that return the function unchanged."""
    class _StubApp:
        def __init__(self, *a, **kw):
            self.config = {}

        def _route(self, *a, **kw):
            return lambda fn: fn

        get = post = _route

    stub = types.ModuleType("flask")
    stub.Flask = _StubApp
    for name in ("Response", "abort", "jsonify", "render_template", "request",
                 "send_file"):
        setattr(stub, name, lambda *a, **kw: None)
    saved = sys.modules.get("flask")
    sys.modules["flask"] = stub
    try:
        path = os.path.join(ROOT, "webapp", "app.py")
        spec = importlib.util.spec_from_file_location("ldz_webapp", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            del sys.modules["flask"]
        else:
            sys.modules["flask"] = saved


app = _load_app()


class RunBuildTest(unittest.TestCase):
    def setUp(self):
        self.job = tempfile.mkdtemp(prefix="ldz-test-job-")
        # create_job() writes this before starting the thread.
        with open(os.path.join(self.job, "status"), "w") as f:
            f.write("RUNNING")

    def status(self):
        with open(os.path.join(self.job, "status")) as f:
            return f.read().strip()

    def log(self):
        with open(os.path.join(self.job, "log.txt")) as f:
            return f.read()

    def test_missing_binary_reports_failure(self):
        """A command that cannot even start must end the job, not hang it."""
        app._run_build(self.job, ["ldz-no-such-binary", "old.zip", "new.zip"])
        self.assertEqual(self.status(), "FAIL")
        self.assertIn("could not run", self.log())

    def test_ran_but_produced_no_pdf(self):
        app._run_build(self.job, [sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(self.status(), "FAIL")
        self.assertIn("exit code 3", self.log())

    def test_pdf_produced_is_done(self):
        out = os.path.join(self.job, "diff.pdf")
        app._run_build(self.job, [sys.executable, "-c",
                                  f"open({out!r}, 'wb').close()"])
        self.assertEqual(self.status(), "DONE")

    def test_status_written_even_if_logging_breaks(self):
        """Whatever _build raises, the job still ends in a readable state."""
        original = app._build
        app._build = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            app._run_build(self.job, ["irrelevant"])
        finally:
            app._build = original
        self.assertEqual(self.status(), "FAIL")


class StagedUploadTest(unittest.TestCase):
    """The staging area POST /jobs resolves its two sides from.

    Each archive is uploaded once, by /inspect, and the build request names it
    by id -- so no single request carries both. Sending both together made
    their sizes add up against the reverse proxy's body cap (100 MB on a
    Cloudflare free plan): two 55 MB projects, each fine alone, were rejected
    at the edge with a 413 the app never saw."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ldz-test-uploads-")
        self.saved_root = app.UPLOADS_ROOT
        app.UPLOADS_ROOT = self.root

    def tearDown(self):
        app.UPLOADS_ROOT = self.saved_root

    def stage(self, upload_id, name="archive.zip"):
        d = os.path.join(self.root, upload_id)
        os.makedirs(d)
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(b"PK\x05\x06" + b"\0" * 18)
        return path

    def test_resolves_a_staged_upload(self):
        path = self.stage("a" * 32, "bug_test_old.zip")
        self.assertEqual(app._staged_path("a" * 32), path)

    def test_unknown_id_is_not_an_error_path(self):
        """An id whose file is gone (cleaned up) resolves to nothing."""
        self.assertIsNone(app._staged_path("b" * 32))

    def test_non_hex_ids_are_refused(self):
        """The id indexes a directory name, so it must not carry a path."""
        self.stage("c" * 32)
        for bad in ("../" + "c" * 32, "c" * 31 + "/", "", "..", "C" * 32):
            self.assertIsNone(app._staged_path(bad), bad)

    def test_cleanup_drops_only_stale_uploads(self):
        """A file picked and never built must not sit in staging forever."""
        fresh = self.stage("d" * 32)
        stale = self.stage("e" * 32)
        old = time.time() - 7200
        os.utime(os.path.dirname(stale), (old, old))
        app._cleanup_old_uploads(max_age=3600)
        self.assertTrue(os.path.exists(fresh))
        self.assertIsNone(app._staged_path("e" * 32))

    def test_safe_name_keeps_the_extension_the_cli_dispatches_on(self):
        """extract_archive() picks its unpacker from the suffix, so it stays."""
        self.assertEqual(app._safe_name("bug_test_old.zip", ".zip"), "bug_test_old.zip")
        self.assertEqual(app._safe_name("v2 final.tar.gz", ".tar.gz"), "v2_final.tar.gz")

    def test_safe_name_strips_paths_and_odd_characters(self):
        self.assertEqual(app._safe_name("../../etc/passwd.zip", ".zip"), "passwd.zip")
        self.assertEqual(app._safe_name("../.hidden.zip", ".zip"), "hidden.zip")
        # Nothing usable left: fall back to a name with the right suffix.
        self.assertEqual(app._safe_name("", ".zip"), "archive.zip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
