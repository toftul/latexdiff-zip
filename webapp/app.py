"""Tiny web front-end for latexdiff-zip.

Uploads two project zips, runs the `latexdiff-zip` CLI (already on PATH in the
container image) as a background job, streams its log to the browser live via
Server-Sent Events, and serves the resulting diff PDF when it is ready.
"""

import os
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
    old = request.files.get("old_zip")
    new = request.files.get("new_zip")
    if not old or not new or not old.filename or not new.filename:
        return jsonify(error="Please choose both the OLD and the NEW .zip files."), 400

    diff_type = request.form.get("diff_type", "UNDERLINE")
    if diff_type not in DIFF_TYPES:
        diff_type = "UNDERLINE"
    main_tex = (request.form.get("main_tex") or "").strip()
    embed_figs = request.form.get("embed_figs") == "on"

    _cleanup_old_jobs()

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_ROOT, job_id)
    os.makedirs(job_dir)

    old_path = os.path.join(job_dir, "old.zip")
    new_path = os.path.join(job_dir, "new.zip")
    out_path = os.path.join(job_dir, "diff.pdf")
    old.save(old_path)
    new.save(new_path)

    cmd = ["latexdiff-zip", "-o", out_path, "-t", diff_type]
    if main_tex:
        cmd += ["-m", main_tex]
    if not embed_figs:
        cmd += ["-F"]
    cmd += [old_path, new_path]

    open(os.path.join(job_dir, "status"), "w").write("RUNNING")
    _log(f"job {job_id} started: {diff_type}, embed={embed_figs}, main={main_tex or 'auto'}")
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
