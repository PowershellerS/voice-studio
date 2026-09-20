#!/usr/bin/env python3
"""Voice Studio — local web app: upload a video, enhance the voice, download it.

Uploads are chunked: the browser sends the file in small pieces instead of
one giant request. This matters on hosted platforms (Render, Railway, etc.)
whose reverse proxy enforces a fixed request timeout (commonly ~100s) that
a single large-file upload over a slow connection can easily exceed,
producing an opaque 502/504 with no useful error. Each chunk request is
small and fast regardless of total file size or connection speed.
"""

import os
import uuid
import threading
import traceback

from flask import Flask, request, jsonify, send_from_directory, render_template

from enhance import enhance_video, PRESETS, DEFAULT_PRESET, EnhanceError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
# Free hosting tiers have limited disk/RAM, so default lower than a local-only
# deployment would need. Override with the MAX_UPLOAD_MB env var if you have
# more headroom (e.g. running this on your own machine).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", 500)) * 1024 * 1024
# Per-chunk request size cap, generous headroom over the ~4MB chunks the
# frontend actually sends.
MAX_CHUNK_BYTES = 12 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CHUNK_BYTES

# In-memory job registry: job_id -> dict(status, progress, stage, error, result, ...)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def _cleanup(job_id: str, in_path: str | None):
    if in_path:
        try:
            os.remove(in_path)
        except OSError:
            pass


@app.route("/")
def index():
    presets = [
        {"key": p.key, "label": p.label, "description": p.description}
        for p in PRESETS.values()
    ]
    return render_template("index.html", presets=presets, default_preset=DEFAULT_PRESET)


@app.route("/api/upload/start", methods=["POST"])
def upload_start():
    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    preset_key = data.get("preset", DEFAULT_PRESET)

    if not filename:
        return jsonify({"error": "No filename provided."}), 400
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXT))}"}), 400
    if preset_key not in PRESETS:
        return jsonify({"error": f"Unknown preset '{preset_key}'."}), 400

    job_id = uuid.uuid4().hex[:12]
    in_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    open(in_path, "wb").close()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "uploading",
            "progress": 0,
            "stage": "uploading",
            "error": None,
            "result": None,
            "in_path": in_path,
            "preset_key": preset_key,
            "output_filename": f"{job_id}_enhanced.mp4",
            "original_filename": filename,
            "bytes_received": 0,
        }
    return jsonify({"job_id": job_id})


@app.route("/api/upload/chunk/<job_id>", methods=["POST"])
def upload_chunk(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "uploading":
        return jsonify({"error": "Unknown or already-finalized upload."}), 404

    chunk = request.get_data()
    if not chunk:
        return jsonify({"error": "Empty chunk received."}), 400

    new_total = job["bytes_received"] + len(chunk)
    if new_total > MAX_UPLOAD_BYTES:
        _cleanup(job_id, job["in_path"])
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify({"error": f"File exceeds the {limit_mb}MB upload limit."}), 413

    with open(job["in_path"], "ab") as f:
        f.write(chunk)

    _set_job(job_id, bytes_received=new_total)
    return jsonify({"received": new_total})


@app.route("/api/upload/finish/<job_id>", methods=["POST"])
def upload_finish(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "uploading":
        return jsonify({"error": "Unknown or already-finalized upload."}), 404
    if job["bytes_received"] == 0:
        return jsonify({"error": "No data was received for this upload."}), 400

    in_path = job["in_path"]
    out_path = os.path.join(OUTPUT_DIR, job["output_filename"])
    preset_key = job["preset_key"]

    _set_job(job_id, status="queued", stage="queued", progress=0)

    def worker():
        _set_job(job_id, status="running")

        def cb(stage, pct):
            _set_job(job_id, stage=stage, progress=pct)

        try:
            result = enhance_video(in_path, out_path, preset_key, progress_cb=cb)
            _set_job(job_id, status="done", progress=100, stage="done", result=result)
        except EnhanceError as e:
            _set_job(job_id, status="error", error=str(e))
        except Exception:
            _set_job(job_id, status="error", error="Unexpected error:\n" + traceback.format_exc(limit=3))
        finally:
            _cleanup(job_id, in_path)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404
    # Don't leak internal filesystem paths to the client.
    safe = {k: v for k, v in job.items() if k not in ("in_path", "preset_key")}
    return jsonify(safe)


@app.route("/api/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "File not ready."}), 404
    return send_from_directory(
        OUTPUT_DIR, job["output_filename"], as_attachment=True,
        download_name=f"enhanced_{job['original_filename'].rsplit('.', 1)[0]}.mp4",
    )


@app.route("/media/output/<job_id>")
def media_output(job_id):
    """Serve the processed file inline (for the <video> preview player)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "File not ready."}), 404
    return send_from_directory(OUTPUT_DIR, job["output_filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"Voice Studio running at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
