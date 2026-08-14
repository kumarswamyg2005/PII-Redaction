"""
Web front end for the redaction pipeline.

The pipeline is built once at import and reused for every request: loading the
zero-shot model takes about twenty seconds, which is fine at boot and
unacceptable per upload.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from pii_redaction import RedactionPipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))
OUTPUT_FOLDER = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "outputs"))
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

#: Only .docx. The tool edits the original file in place to preserve its
#: formatting, which a PDF-to-text pipeline cannot do.
ALLOWED_EXTENSIONS = {".docx"}

logger.info("loading redaction pipeline (this takes a moment on first start)...")
pipeline = RedactionPipeline()
logger.info("pipeline ready")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "service": "pii-redaction"})


#: Jobs in flight and recently finished, newest last.
#:
#: Redacting a 126-page document takes around two minutes. Answering the upload
#: request only when that finishes means holding an HTTP connection open for the
#: whole run, and anything in the path is entitled to give up first — a tunnel,
#: a proxy, a load balancer, the browser itself. When one does, the work is lost
#: and the user sees a connection error rather than a result. So the upload
#: returns immediately with a job id and the client polls; no request is ever
#: open for more than a moment, and the run finishes regardless.
#:
#: This lives in memory on purpose. It is correct for exactly the deployment
#: this ships with — a single worker, threads shared (see the Dockerfile) — and
#: needs no broker, no database and no second process. Running more than one
#: worker would split the store and break status lookups; that is the one thing
#: to remember if this ever moves off a laptop.
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()
#: Finished jobs kept for the client to collect. Enough for any real session;
#: bounded so a long-running server cannot grow without limit.
MAX_JOBS = 50


def _set(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _run_job(job_id: str, source: Path, destination: Path,
             filename: str, output_name: str) -> None:
    """Redact in a worker thread and record the outcome against the job id."""
    try:
        outcome = pipeline.run(source, destination)
    except Exception as exc:  # pragma: no cover - surfaced through /status
        logger.exception("redaction failed for job %s", job_id)
        _set(job_id, state="error", error=str(exc))
    else:
        _set(
            job_id,
            state="done",
            original_filename=filename,
            output_filename=output_name,
            download_url=f"/download/{output_name}",
            summary={
                "entities_redacted": len(outcome.detections),
                "suppressed_by_policy": len(outcome.suppressed),
                "distinct_entities": len(outcome.mapping),
                "replacements": outcome.total_replacements,
                "images_redacted": len(outcome.image_findings),
                "defined_terms_learned": outcome.defined_terms_learned,
                "seconds": round(outcome.seconds, 1),
                "entity_counts": outcome.entity_counts,
            },
        )
    finally:
        source.unlink(missing_ok=True)


@app.route("/redact", methods=["POST"])
def redact():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "No file was uploaded."}), 400

    filename = secure_filename(uploaded.filename)
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Only .docx files are supported."}), 400

    job_id = uuid.uuid4().hex[:8]
    source = UPLOAD_FOLDER / f"{job_id}_{filename}"
    output_name = f"{Path(filename).stem}-redacted-{job_id}.docx"
    uploaded.save(source)

    with _jobs_lock:
        while len(_jobs) >= MAX_JOBS:
            _jobs.popitem(last=False)
        _jobs[job_id] = {"state": "running", "started": time.time()}

    threading.Thread(
        target=_run_job,
        args=(job_id, source, OUTPUT_FOLDER / output_name, filename, output_name),
        daemon=True,
    ).start()

    # 202: accepted, not finished. The client polls status_url.
    return jsonify({
        "status": "accepted",
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
    }), 202


@app.route("/status/<job_id>")
def status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        job = dict(job) if job else None
    if job is None:
        return jsonify({"state": "unknown"}), 404
    if job.get("state") == "running":
        job["elapsed"] = round(time.time() - job["started"], 1)
    job.pop("started", None)
    return jsonify(job)


@app.route("/download/<path:filename>")
def download(filename: str):
    return send_from_directory(
        OUTPUT_FOLDER, secure_filename(filename), as_attachment=True
    )


if __name__ == "__main__":
    # 7860 is the port Hugging Face Spaces expects.
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
