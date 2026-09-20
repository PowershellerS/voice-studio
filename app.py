#!/usr/bin/env python3
"""Voice Studio — local web app: upload a video, enhance the voice, download it."""

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
MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2 GB

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# In-memory job registry: job_id -> dict(status, progress, stage, error, result, filename)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


@app.route("/")
def index():
    presets = [
        {"key": p.key, "label": p.label, "description": p.description}
        for p in PRESETS.values()
    ]
    return render_template("index.html", presets=presets, default_preset=DEFAULT_PRESET)


@app.route("/api/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "No file selected."}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXT))}"}), 400

    preset_key = request.form.get("preset", DEFAULT_PRESET)
    if preset_key not in PRESETS:
        return jsonify({"error": f"Unknown preset '{preset_key}'."}), 400

    job_id = uuid.uuid4().hex[:12]
    in_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}_enhanced.mp4")
    file.save(in_path)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "error": None,
            "result": None,
            "output_filename": os.path.basename(out_path),
            "original_filename": file.filename,
        }

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
            try:
                os.remove(in_path)
            except OSError:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404
    return jsonify(job)


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
