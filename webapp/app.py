"""Tiny web front-end for latexdiff-zip.

Takes two project versions — each an uploaded archive or an arXiv id — runs
the `latexdiff-zip` CLI (already on PATH in the container image) as a
background job, streams its log to the browser live via Server-Sent Events,
and serves the resulting diff PDF when it is ready.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

import mains

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per request

# Largest single archive the UI will try to send. This is not the app's own
# limit (MAX_CONTENT_LENGTH above is) but the *edge's*: a reverse proxy in front
# of the app caps request bodies -- a Cloudflare free plan at 100 MB -- and
# rejects a bigger one with a 413 the app never sees. The browser checks each
# file against this before uploading, so an oversized archive is named as such
# straight away instead of failing somewhere upstream. Raise it via
# LDZ_MAX_UPLOAD_MB when the deployment in front of the app allows more.
MAX_UPLOAD_MB = int(os.environ.get("LDZ_MAX_UPLOAD_MB", "100"))

# latexdiff --type values worth offering in the UI, each with the plain-English
# label the dropdown shows. The raw tag stays in the label (and is the option's
# value) so it still lines up with the CLI's -t flag and latexdiff's own docs.
DIFF_TYPE_LABELS = [
    ("UNDERLINE", "Added underlined, deleted struck out (UNDERLINE)"),
    ("CFONT", "Added blue, deleted red & small (CFONT)"),
    ("CCHANGEBAR", "Colored + bars in the margin (CCHANGEBAR)"),
    ("CULINECHBAR", "Underline/strikeout + bars in the margin (CULINECHBAR)"),
    ("BOLD", "Added in bold, deletions hidden (BOLD)"),
]
DIFF_TYPES = [t for t, _ in DIFF_TYPE_LABELS]

# Archive extensions the CLI can extract. The CLI dispatches on the file
# extension, so an upload must be saved under a name that ends in one of these
# (longest match first, so .tar.gz wins over .gz-style suffixes).
ARCHIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar", ".zip")


def _archive_ext(filename):
    """Return the recognised archive suffix of an upload, or '' if unsupported."""
    name = (filename or "").lower()
    for ext in ARCHIVE_EXTS:
        if name.endswith(ext):
            return ext
    return ""


def _safe_name(filename, ext):
    """A staged upload's on-disk name: the user's, reduced to safe characters.

    It only has to be readable in the log and end in the extension the CLI
    dispatches on -- the file sits alone in a uuid-named directory, so the name
    itself carries no uniqueness."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "")).lstrip(".")
    return base if base.lower().endswith(ext) else "archive" + ext


# Reuse the engine's arXiv id/URL normaliser so there is one implementation,
# not a copy that can drift. The engine is latexdiff-zip.py: the repo-local file
# in dev, or the installed `latexdiff-zip` on PATH inside the container.
def _load_normalize_arxiv():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.normpath(os.path.join(here, "..", "latexdiff-zip.py")),
                 shutil.which("latexdiff-zip")):
        if not path or not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("ldz_engine", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)   # import-only: the engine guards main()
            fn = getattr(mod, "normalize_arxiv", None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


# Fallback used only if the engine file can't be located/imported, so arXiv
# validation never hard-fails. Kept minimal; the engine is the source of truth.
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?/\d{7})(v\d+)?")


def _fallback_arxiv_id(raw):
    s = (raw or "").split("?", 1)[0]
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(www\.|export\.)", "", s)
    m = re.match(r"arxiv\.org/[^/]+/(.+)", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    s = re.sub(r"^arxiv:", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.pdf$", "", s, flags=re.IGNORECASE)
    return s if _ARXIV_ID_RE.fullmatch(s) else None


normalize_arxiv = _load_normalize_arxiv() or _fallback_arxiv_id

# How long a single build is allowed to run before we give up.
BUILD_TIMEOUT = int(os.environ.get("LDZ_TIMEOUT", "600"))

# Per-job scratch space; shared across worker threads (run gunicorn with 1 worker).
JOBS_ROOT = tempfile.mkdtemp(prefix="ldz-jobs-")

# Where an archive lands when the UI scans it on selection. Each side is
# uploaded once, here, and the later POST /jobs just names the two by id -- so
# no single request carries both archives, and neither is sent twice. See
# stage_upload() for why that matters.
UPLOADS_ROOT = tempfile.mkdtemp(prefix="ldz-uploads-")


def _log(msg):
    """Print to the server's stdout so `podman logs` shows activity too."""
    print(f"[latexdiff-zip-web] {msg}", file=sys.stderr, flush=True)


def _job_dir(job_id):
    # Guard against path traversal: ids are hex from uuid4, but be strict.
    if not job_id or not all(c in "0123456789abcdef" for c in job_id):
        abort(404)
    d = os.path.join(JOBS_ROOT, job_id)
    if not os.path.isdir(d):
        abort(404)
    return d


def _cleanup_old_jobs(max_age=3600):
    """Best-effort removal of job dirs older than max_age seconds."""
    now = time.time()
    try:
        for name in os.listdir(JOBS_ROOT):
            p = os.path.join(JOBS_ROOT, name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _staged_path(upload_id):
    """Path of a staged upload, or None if the id names nothing we stored."""
    if not upload_id or not all(c in "0123456789abcdef" for c in upload_id):
        return None
    d = os.path.join(UPLOADS_ROOT, upload_id)
    if not os.path.isdir(d):
        return None
    names = os.listdir(d)
    return os.path.join(d, names[0]) if names else None


def _cleanup_old_uploads(max_age=3600):
    """Drop staged archives nobody turned into a job. Same policy as job dirs.

    A file picked and then replaced (or a page closed before Generate) leaves
    its upload behind, so this has to run on its own clock rather than as part
    of job cleanup."""
    now = time.time()
    try:
        for name in os.listdir(UPLOADS_ROOT):
            p = os.path.join(UPLOADS_ROOT, name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _build(job_dir, cmd):
    """Run the CLI, streaming combined output into log.txt; return DONE/FAIL."""
    log_path = os.path.join(job_dir, "log.txt")
    out_path = os.path.join(job_dir, "diff.pdf")

    with open(log_path, "w", buffering=1) as log:
        log.write("$ " + " ".join(cmd[:1]) + " … " + " ".join(cmd[-2:]) + "\n")
        proc = subprocess.Popen(
            cmd,
            cwd=job_dir,
            env={**os.environ, "HOME": "/tmp"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Kill the build if it overruns the timeout.
        timed_out = {"hit": False}

        def _kill():
            timed_out["hit"] = True
            proc.kill()

        watchdog = threading.Timer(BUILD_TIMEOUT, _kill)
        watchdog.start()
        try:
            for line in proc.stdout:
                log.write(line)
            proc.wait()
        finally:
            watchdog.cancel()

        if timed_out["hit"]:
            log.write(f"\n!! build timed out after {BUILD_TIMEOUT}s\n")
            status = "FAIL"
        elif os.path.exists(out_path):
            status = "DONE"
        else:
            log.write(f"\n!! build failed (exit code {proc.returncode}), no PDF produced\n")
            status = "FAIL"

    return status


def _run_build(job_dir, cmd):
    """Run one build and *always* leave a status behind.

    The SSE stream in job_events polls the status file and only stops on
    DONE/FAIL, so a build thread that died before writing one -- the CLI
    missing from PATH, a full disk, anything raising out of _build -- would
    leave the browser watching a spinner that never resolves. Record the
    failure instead, and put the reason in the log the UI shows."""
    status = "FAIL"
    try:
        status = _build(job_dir, cmd)
    except Exception as exc:
        _log(f"job {os.path.basename(job_dir)} crashed: {exc!r}")
        try:
            with open(os.path.join(job_dir, "log.txt"), "a") as log:
                log.write(f"\n!! build could not run: {exc}\n")
        except OSError:
            pass
    finally:
        with open(os.path.join(job_dir, "status"), "w") as f:
            f.write(status)
    _log(f"job {os.path.basename(job_dir)} -> {status}")


@app.get("/")
def index():
    return render_template("index.html", diff_types=DIFF_TYPE_LABELS,
                           max_upload_mb=MAX_UPLOAD_MB)


@app.post("/inspect")
def inspect():
    """Stage one archive and name the plausible main .tex files inside it.

    Called as soon as a file is chosen. It does two jobs at once, deliberately:

    * it fills the side's main-.tex dropdown, so an archive holding several
      \\documentclass files is resolved with a click rather than a failed build;
    * it *keeps* the archive and hands back an id, so the later POST /jobs is a
      few hundred bytes naming two staged files instead of a request carrying
      both of them.

    That split is what makes big projects work. Sending both archives in one
    request meant their sizes added up against the reverse proxy's body cap
    (100 MB on a Cloudflare free plan), so two 55 MB projects -- each fine on
    its own -- were rejected at the edge with a 413 the app never saw, and the
    page sat on an empty log. It also means each archive goes over the wire
    once, not once to be scanned and again to be built."""
    upload = request.files.get("archive")
    if not upload or not upload.filename:
        return jsonify(error="No archive was sent."), 400
    ext = _archive_ext(upload.filename)
    if not ext:
        return jsonify(
            error="The upload must be a .zip or a tar archive "
                  "(.tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz)."
        ), 400

    _cleanup_old_uploads()

    upload_id = uuid.uuid4().hex
    staged_dir = os.path.join(UPLOADS_ROOT, upload_id)
    os.makedirs(staged_dir)
    path = os.path.join(staged_dir, _safe_name(upload.filename, ext))
    upload.save(path)

    # Scan from the saved copy: the stream has been consumed by save().
    found = mains.candidates(path)
    _log(f"inspect {upload.filename} ({os.path.getsize(path)} bytes) staged as "
         f"{upload_id}: {len(found)} candidate main(s)"
         + (f" ({', '.join(found)})" if found else ""))
    return jsonify(candidates=found, upload=upload_id)


@app.post("/jobs")
def create_job():
    # Each side arrives as an already-staged upload (the normal path: /inspect
    # kept the archive when the file was picked), as an archive posted here, or
    # as an arXiv id to fetch. A staged id wins, then a file, then the id --
    # the UI keeps archive and arXiv mutually exclusive anyway.
    sides = []  # per side: ("staged", path, None) | ("file", FileStorage, ext)
                #           | ("arxiv", id, None)
    for label, staged_id, upload, raw_id in (
        ("OLD", request.form.get("old_upload", ""), request.files.get("old_zip"),
         request.form.get("old_arxiv", "")),
        ("NEW", request.form.get("new_upload", ""), request.files.get("new_zip"),
         request.form.get("new_arxiv", "")),
    ):
        staged = _staged_path(staged_id.strip())
        if staged:
            sides.append(("staged", staged, None))
        elif staged_id.strip():
            # Staged uploads are cleaned up by age, so an id can outlive its
            # file on a page left open for an hour. Say so plainly -- the UI
            # can only fix it by having the user pick the file again.
            return jsonify(
                error=f"The {label} upload has expired. Please choose the file again."
            ), 400
        elif upload and upload.filename:
            # The CLI extracts by extension, so reject anything we can't unpack
            # and keep the real suffix when saving (.tar.gz vs .zip leads to
            # different handling).
            ext = _archive_ext(upload.filename)
            if not ext:
                return jsonify(
                    error=f"The {label} upload must be a .zip or a tar archive "
                          "(.tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz)."
                ), 400
            sides.append(("file", upload, ext))
        elif raw_id.strip():
            arxiv = normalize_arxiv(raw_id.strip())
            if not arxiv:
                return jsonify(
                    error=f"The {label} arXiv reference “{raw_id.strip()}” doesn't "
                          "look like an arXiv id or URL (try e.g. 2401.12345v1)."
                ), 400
            sides.append(("arxiv", arxiv, None))
        else:
            return jsonify(
                error=f"Please provide the {label} version: choose an archive "
                      "or enter an arXiv id."
            ), 400

    diff_type = request.form.get("diff_type", "UNDERLINE")
    if diff_type not in DIFF_TYPES:
        diff_type = "UNDERLINE"
    main_tex_old = (request.form.get("main_tex_old") or "").strip()
    main_tex_new = (request.form.get("main_tex_new") or "").strip()
    embed_figs = request.form.get("embed_figs") == "on"

    _cleanup_old_jobs()

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_ROOT, job_id)
    os.makedirs(job_dir)

    out_path = os.path.join(job_dir, "diff.pdf")

    # Turn each side into a CLI argument: saved upload path, or bare arXiv id
    # (the CLI downloads the e-print itself).
    args, descs = [], []
    for name, (kind, value, ext) in zip(("old", "new"), sides):
        if kind == "staged":
            # Read in place; _cleanup_old_uploads() owns the file, so pressing
            # Generate twice on the same pair works without re-uploading.
            args.append(value)
            descs.append(os.path.basename(value))
        elif kind == "file":
            path = os.path.join(job_dir, name + ext)
            value.save(path)
            args.append(path)
            descs.append(value.filename)
        else:
            args.append(value)
            descs.append(f"arXiv:{value}")

    cmd = ["latexdiff-zip", "-o", out_path, "-t", diff_type]
    if main_tex_old:
        cmd += ["-m", main_tex_old]
    if main_tex_new:
        cmd += ["-M", main_tex_new]
    if not embed_figs:
        cmd += ["-F"]
    cmd += args

    open(os.path.join(job_dir, "status"), "w").write("RUNNING")
    _log(f"job {job_id} started: {diff_type}, embed={embed_figs}, "
         f"main_old={main_tex_old or 'auto'}, main_new={main_tex_new or 'auto'}, "
         f"inputs: {descs[0]} vs {descs[1]}")
    threading.Thread(target=_run_build, args=(job_dir, cmd), daemon=True).start()

    return jsonify(id=job_id)


@app.get("/jobs/<job_id>/events")
def job_events(job_id):
    job_dir = _job_dir(job_id)
    log_path = os.path.join(job_dir, "log.txt")
    status_path = os.path.join(job_dir, "status")

    def stream():
        offset = 0
        buf = ""
        while True:
            try:
                with open(log_path, "r") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except FileNotFoundError:
                chunk = ""
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                yield f"data: {line}\n\n"

            status = "RUNNING"
            try:
                status = open(status_path).read().strip()
            except FileNotFoundError:
                pass
            if status in ("DONE", "FAIL"):
                if buf:  # flush any trailing partial line
                    yield f"data: {buf}\n\n"
                yield f"event: done\ndata: {status}\n\n"
                return
            time.sleep(0.4)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/<job_id>/pdf")
def job_pdf(job_id):
    job_dir = _job_dir(job_id)
    out_path = os.path.join(job_dir, "diff.pdf")
    if not os.path.exists(out_path):
        abort(404)
    return send_file(out_path, mimetype="application/pdf", download_name="diff.pdf")


if __name__ == "__main__":
    # Dev server only; the container uses gunicorn. threaded=True so the SSE
    # stream and the build thread can run alongside each other.
    app.run(host="0.0.0.0", port=8080, debug=True, threaded=True)
