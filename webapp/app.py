"""Tiny web front-end for latexdiff-zip.

Takes two project versions — each an uploaded archive or an arXiv id — runs
the `latexdiff-zip` CLI (already on PATH in the container image) as a
background job, streams its log to the browser live via Server-Sent Events,
and serves the resulting diff PDF when it is ready.
"""

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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per request

# latexdiff --type values worth offering in the UI.
DIFF_TYPES = ["UNDERLINE", "CFONT", "CCHANGEBAR", "CULINECHBAR", "BOLD"]

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


# New-style arXiv ids (YYMM.NNNNN) or old-style ones (archive.SC/YYMMNNN),
# each with an optional vN suffix.
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?/\d{7})(v\d+)?")


def _arxiv_id(raw):
    """Normalise an arXiv reference (bare id, arXiv:<id>, or arxiv.org URL) to
    a bare id like 2401.12345v2, or return None if it doesn't look like one.
    Mirrors normalize_arxiv() in latexdiff-zip.sh; strict validation also keeps
    the value safe to pass to the CLI (it can never start with '-' or '/')."""
    s = (raw or "").strip().split("?", 1)[0]
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(www\.|export\.)", "", s)
    m = re.match(r"arxiv\.org/[^/]+/(.+)", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    s = re.sub(r"^arxiv:", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.pdf$", "", s, flags=re.IGNORECASE)
    return s if _ARXIV_ID_RE.fullmatch(s) else None

# How long a single build is allowed to run before we give up.
BUILD_TIMEOUT = int(os.environ.get("LDZ_TIMEOUT", "600"))

# Per-job scratch space; shared across worker threads (run gunicorn with 1 worker).
JOBS_ROOT = tempfile.mkdtemp(prefix="ldz-jobs-")


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


def _run_build(job_dir, cmd):
    """Run the CLI, streaming combined output into log.txt, then record status."""
    log_path = os.path.join(job_dir, "log.txt")
    status_path = os.path.join(job_dir, "status")
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

    with open(status_path, "w") as f:
        f.write(status)
    _log(f"job {os.path.basename(job_dir)} -> {status}")


@app.get("/")
def index():
    return render_template("index.html", diff_types=DIFF_TYPES)


@app.post("/jobs")
def create_job():
    # Each side arrives as an uploaded archive or as an arXiv id to fetch; an
    # upload wins when both are given (the UI keeps them mutually exclusive).
    sides = []  # per side: ("file", FileStorage, ext) or ("arxiv", id, None)
    for label, upload, raw_id in (
        ("OLD", request.files.get("old_zip"), request.form.get("old_arxiv", "")),
        ("NEW", request.files.get("new_zip"), request.form.get("new_arxiv", "")),
    ):
        if upload and upload.filename:
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
            arxiv = _arxiv_id(raw_id)
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
        if kind == "file":
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
