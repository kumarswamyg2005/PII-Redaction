"""
Web front end for the redaction pipeline.

The pipeline is built once at import and reused for every request: loading the
zero-shot model takes about twenty seconds, which is fine at boot and
unacceptable per upload.
"""

from __future__ import annotations

import logging
import os
import uuid
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


@app.route("/redact", methods=["POST"])
def redact():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "No file was uploaded."}), 400

    filename = secure_filename(uploaded.filename)
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Only .docx files are supported."}), 400

    job = uuid.uuid4().hex[:8]
    source = UPLOAD_FOLDER / f"{job}_{filename}"
    output_name = f"{Path(filename).stem}-redacted-{job}.docx"
    destination = OUTPUT_FOLDER / output_name
    uploaded.save(source)

    try:
        outcome = pipeline.run(source, destination)
    except Exception as exc:  # pragma: no cover - surfaced to the client
        logger.exception("redaction failed")
        return jsonify({"error": f"Redaction failed: {exc}"}), 500
    finally:
        source.unlink(missing_ok=True)

    return jsonify({
        "status": "success",
        "original_filename": filename,
        "output_filename": output_name,
        "download_url": f"/download/{output_name}",
        "summary": {
            "entities_redacted": len(outcome.detections),
            "suppressed_by_policy": len(outcome.suppressed),
            "distinct_entities": len(outcome.mapping),
            "replacements": outcome.total_replacements,
            "images_redacted": len(outcome.image_findings),
            "defined_terms_learned": outcome.defined_terms_learned,
            "seconds": round(outcome.seconds, 1),
            "entity_counts": outcome.entity_counts,
        },
    })


@app.route("/download/<path:filename>")
def download(filename: str):
    return send_from_directory(
        OUTPUT_FOLDER, secure_filename(filename), as_attachment=True
    )


if __name__ == "__main__":
    # 7860 is the port Hugging Face Spaces expects.
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
