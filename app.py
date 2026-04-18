import os
import uuid
import requests
import tempfile
import subprocess
import logging
import traceback
import json
from pathlib import Path
from flask import Flask, request, jsonify


# ── Auth ──────────────────────────────────────────────────────────────────────
AUTH_TOKEN = os.environ.get("API_SECRET_TOKEN")

def check_auth():
    if not AUTH_TOKEN:
        return  # no auth configured, allow all
    token = request.headers.get("X-Api-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != AUTH_TOKEN:
        from flask import abort
        abort(403)


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CLOUDINARY_API_KEY    = os.environ.get("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")

DEFAULT_SCENE_DURATION = 3   # seconds per image

FFMPEG  = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list, cwd=None) -> subprocess.CompletedProcess:
    """Run a shell command, log it, raise on non-zero exit."""
    log.info("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.stdout:
        log.info("STDOUT: %s", result.stdout[-2000:])
    if result.stderr:
        log.info("STDERR: %s", result.stderr[-2000:])
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}):\n"
            f"CMD : {' '.join(cmd)}\n"
            f"STDERR: {result.stderr[-1000:]}"
        )
    return result


def download_file(url: str, dest: Path) -> Path:
    """Stream-download a URL to dest path."""
    log.info("Downloading %s -> %s", url, dest)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    log.info("Downloaded %s bytes", dest.stat().st_size)
    return dest


def upload_to_cloudinary(file_path: Path) -> str:
    """Upload a file to Cloudinary and return the secure_url."""
    import hashlib
    import time

    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise RuntimeError(
            "Cloudinary env vars missing: CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET"
        )

    timestamp = str(int(time.time()))
    params_to_sign = f"timestamp={timestamp}"
    signature = hashlib.sha1(
        (params_to_sign + CLOUDINARY_API_SECRET).encode()
    ).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    log.info("Uploading %s to Cloudinary ...", file_path)

    with open(file_path, "rb") as fh:
        resp = requests.post(
            url,
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "signature": signature,
            },
            files={"file": fh},
            timeout=300,
        )
    resp.raise_for_status()
    data = resp.json()
    log.info("Cloudinary response: %s", json.dumps(data)[:500])
    return data["secure_url"]


def build_video(
    image_paths: list,
    audio_path: Path,
    output_path: Path,
    scene_duration: int = DEFAULT_SCENE_DURATION,
) -> Path:
    """
    Assemble portrait video from images + audio using FFmpeg.
    720x1280 (9:16) to stay within Railway RAM limits.
    """
    workdir = output_path.parent

    # ── Step 0: probe audio duration ──────────────────────────────────────────
    probe = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    try:
        audio_duration = float(probe.stdout.strip())
    except ValueError:
        audio_duration = len(image_paths) * scene_duration
    log.info("Audio duration: %.2f s", audio_duration)

    # ── Step 1: per-image clips at 720x1280 ───────────────────────────────────
    clip_paths = []
    for idx, img in enumerate(image_paths):
        clip = workdir / f"clip_{idx:03d}.mp4"
        run([
            FFMPEG, "-y",
            "-loop", "1",
            "-i", str(img),
            "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
            "-t", str(scene_duration),
            "-r", "24",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-crf", "30",
            str(clip),
        ])
        clip_paths.append(clip)

    # ── Step 2: concat clips ──────────────────────────────────────────────────
    concat_file = workdir / "concat.txt"
    concat_file.write_text("\n".join(f"file '{p}'" for p in clip_paths))
    raw_video = workdir / "raw_video.mp4"
    run([
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(raw_video),
    ])

    # ── Step 3: loop video to audio length, mux audio ─────────────────────────
    run([
        FFMPEG, "-y",
        "-stream_loop", "-1", "-i", str(raw_video),
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])

    log.info("Video built: %s (%.1f MB)", output_path, output_path.stat().st_size / 1e6)
    return output_path


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Liveness probe."""
    try:
        run([FFMPEG, "-version"])
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False
    return jsonify({"status": "ok", "ffmpeg": ffmpeg_ok}), 200

@app.route("/debug", methods=["GET"])
def debug():
    checks = {}
    for path in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        checks[path] = os.path.exists(path)
    
    result = subprocess.run(
        ["which", "ffmpeg"],
        capture_output=True, text=True
    )
    which_result = subprocess.run(
        ["ls", "/usr/bin/ff*"],
        capture_output=True, text=True,
        shell=False
    )
    return jsonify({
        "path_checks": checks,
        "which_ffmpeg": result.stdout.strip(),
        "PATH": os.environ.get("PATH")
    }), 200

@app.route("/build", methods=["POST"])
def build():
    """
    Build a short-form video.

    Expected JSON body:
    {
        "video_id":       "abc123",
        "audio_url":      "https://...",
        "image_urls":     ["https://...", ...],
        "scene_duration": 3
    }

    Returns:
    { "video_url": "https://res.cloudinary.com/..." }
    """
    check_auth()

    try:
        body = request.get_json(force=True, silent=True) or {}
        log.info("Received /build payload: %s", json.dumps(body)[:500])
    except Exception:
        body = {}

    video_id   = body.get("video_id") or str(uuid.uuid4())
    audio_url  = body.get("audio_url", "").strip()
    image_urls = body.get("image_urls") or []
    scene_dur  = int(body.get("scene_duration", DEFAULT_SCENE_DURATION))

    errors = []
    if not audio_url:
        errors.append("'audio_url' is required")
    if not image_urls:
        errors.append("'image_urls' must be a non-empty list")
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    with tempfile.TemporaryDirectory(prefix="vb_") as tmpdir:
        tmp = Path(tmpdir)

        try:
            # Download audio
            audio_ext  = Path(audio_url.split("?")[0]).suffix or ".mp3"
            audio_path = tmp / f"audio{audio_ext}"
            download_file(audio_url, audio_path)

            # Download images
            image_paths = []
            for i, url in enumerate(image_urls[:8]):
                ext = Path(url.split("?")[0]).suffix or ".jpg"
                p   = tmp / f"img_{i:03d}{ext}"
                download_file(url, p)
                image_paths.append(p)

            if not image_paths:
                return jsonify({"error": "No images could be downloaded"}), 500

            # Build video
            output_path = tmp / f"{video_id}.mp4"
            build_video(image_paths, audio_path, output_path, scene_dur)

            # Upload to Cloudinary
            video_url = upload_to_cloudinary(output_path)

            return jsonify({"video_url": video_url, "video_id": video_id}), 200

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Build failed for video_id=%s:\n%s", video_id, tb)
            return jsonify({"error": str(exc), "traceback": tb[-2000:]}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
