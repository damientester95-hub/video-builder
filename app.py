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
        return
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

FFMPEG  = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

# Hard cap: never encode more than 60s of video total
MAX_VIDEO_SECONDS = 60


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list, cwd=None) -> subprocess.CompletedProcess:
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
    log.info("Downloading %s -> %s", url, dest)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    log.info("Downloaded %s bytes", dest.stat().st_size)
    return dest


def upload_to_cloudinary(file_path: Path) -> str:
    import hashlib, time
    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise RuntimeError("Cloudinary env vars missing")
    timestamp = str(int(time.time()))
    signature = hashlib.sha1(
        (f"timestamp={timestamp}" + CLOUDINARY_API_SECRET).encode()
    ).hexdigest()
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    log.info("Uploading %s to Cloudinary ...", file_path)
    with open(file_path, "rb") as fh:
        resp = requests.post(
            url,
            data={"api_key": CLOUDINARY_API_KEY, "timestamp": timestamp, "signature": signature},
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
    scene_duration: int = 5,
) -> Path:
    """
    Build a portrait video.
    - Each image shows for exactly scene_duration seconds
    - Total video = scene_duration * num_images (hard capped at MAX_VIDEO_SECONDS)
    - Audio is trimmed to match video length
    - 720x1280 portrait, ultrafast encoding
    """
    workdir = output_path.parent
    n = len(image_paths)

    # Cap total duration to avoid OOM / timeout
    per_image = min(scene_duration, MAX_VIDEO_SECONDS // n)
    total = per_image * n
    log.info("Building video: %d images x %ds = %ds total", n, per_image, total)

    # ── Step 1: one clip per image ─────────────────────────────────────────────
    clip_paths = []
    for idx, img in enumerate(image_paths):
        clip = workdir / f"clip_{idx:03d}.mp4"
        run([
            FFMPEG, "-y",
            "-loop", "1",
            "-i", str(img),
            "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
            "-t", str(per_image),
            "-r", "24",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-crf", "30",
            "-tune", "stillimage",
            str(clip),
        ])
        clip_paths.append(clip)

    # ── Step 2: concat clips ───────────────────────────────────────────────────
    concat_file = workdir / "concat.txt"
    concat_file.write_text("\n".join(f"file '{p}'" for p in clip_paths))
    silent_video = workdir / "silent.mp4"
    run([
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(silent_video),
    ])

    # ── Step 3: mux audio, trim both to total duration ─────────────────────────
    run([
        FFMPEG, "-y",
        "-i", str(silent_video),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", str(total),
        "-movflags", "+faststart",
        str(output_path),
    ])

    log.info("Video built: %s (%.1f MB)", output_path, output_path.stat().st_size / 1e6)
    return output_path


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    try:
        run([FFMPEG, "-version"])
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False
    return {"status": "ok", "ffmpeg": ffmpeg_ok}, 200


@app.route("/build", methods=["POST"])
def build():
    check_auth()

    try:
        body = request.get_json(force=True, silent=True) or {}
        log.info("Received /build payload: %s", json.dumps(body)[:500])
    except Exception:
        body = {}

    video_id   = body.get("video_id") or str(uuid.uuid4())
    audio_url  = body.get("audio_url", "").strip()
    image_urls = body.get("image_urls") or []
    scene_dur  = int(body.get("scene_duration", 5))

    errors = []
    if not audio_url:
        errors.append("'audio_url' is required")
    if not image_urls:
        errors.append("'image_urls' must be a non-empty list")
    if errors:
        return {"error": "; ".join(errors)}, 400

    with tempfile.TemporaryDirectory(prefix="vb_") as tmpdir:
        tmp = Path(tmpdir)
        try:
            audio_ext  = Path(audio_url.split("?")[0]).suffix or ".mp3"
            audio_path = tmp / f"audio{audio_ext}"
            download_file(audio_url, audio_path)

            image_paths = []
            for i, url in enumerate(image_urls[:8]):
                ext = Path(url.split("?")[0]).suffix or ".jpg"
                p   = tmp / f"img_{i:03d}{ext}"
                download_file(url, p)
                image_paths.append(p)

            if not image_paths:
                return {"error": "No images could be downloaded"}, 500

            output_path = tmp / f"{video_id}.mp4"
            build_video(image_paths, audio_path, output_path, scene_dur)

            video_url = upload_to_cloudinary(output_path)
            return {"video_url": video_url, "video_id": video_id}, 200

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Build failed for video_id=%s:\n%s", video_id, tb)
            return {"error": str(exc), "traceback": tb[-2000:]}, 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
