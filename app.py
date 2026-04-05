from flask import Flask, request, jsonify
import requests
import cloudinary
import cloudinary.uploader
import os
import uuid
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import *
from moviepy.video.fx.all import fadein, fadeout
import tempfile
import urllib.request

app = Flask(__name__)

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

SCENE_DURATION = 7
FONT_SIZE = 60
VIDEO_W = 720
VIDEO_H = 1280

def download_file(url, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    urllib.request.urlretrieve(url, tmp.name)
    return tmp.name

def make_text_clip(text, duration, fontsize=FONT_SIZE, position=("center", "center")):
    return (TextClip(
        text,
        fontsize=fontsize,
        color="white",
        stroke_color="black",
        stroke_width=3,
        font="Montserrat-Bold",
        method="caption",
        size=(VIDEO_W - 80, None),
        align="center"
    )
    .set_duration(duration)
    .set_position(position)
    .fadein(0.3)
    .fadeout(0.3))

def make_image_clip(image_url, duration):
    path = download_file(image_url, ".jpg")
    img = Image.open(path).convert("RGB")
    
    # Crop to 9:16
    img_w, img_h = img.size
    target_ratio = VIDEO_W / VIDEO_H
    if img_w / img_h > target_ratio:
        new_w = int(img_h * target_ratio)
        left = (img_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, img_h))
    else:
        new_h = int(img_w / target_ratio)
        top = (img_h - new_h) // 2
        img = img.crop((0, top, img_w, top + new_h))
    
    img = img.resize((VIDEO_W, VIDEO_H), Image.LANCZOS)
    img.save(path)
    
    clip = ImageClip(path).set_duration(duration)
    
    # Ken Burns zoom effect
    def zoom(t):
        scale = 1 + 0.04 * (t / duration)
        new_w = int(VIDEO_W * scale)
        new_h = int(VIDEO_H * scale)
        x = (new_w - VIDEO_W) // 2
        y = (new_h - VIDEO_H) // 2
        return clip.resize((new_w, new_h)).crop(x1=x, y1=y, x2=x+VIDEO_W, y2=y+VIDEO_H).get_frame(t)
    
    return VideoClip(zoom, duration=duration).set_fps(30)

@app.route("/build", methods=["POST"])
def build_video():
    data = request.json
    
    title = data.get("title", "")
    hook = data.get("hook", "")
    captions = data.get("captions", "")
    voiceover_url = data.get("voiceover_url", "")
    images = data.get("images", [])
    video_id = data.get("video_id", str(uuid.uuid4()))

    if len(images) < 4:
        return jsonify({"error": "Need 4 image URLs"}), 400

    try:
        # Build each scene
        scenes = []

        # Scene 1 - Title
        img1 = make_image_clip(images[0], SCENE_DURATION)
        txt1 = make_text_clip(title, SCENE_DURATION, fontsize=65, position=("center", "center"))
        scene1 = CompositeVideoClip([img1, txt1], size=(VIDEO_W, VIDEO_H))
        scenes.append(scene1)

        # Scene 2 - Hook
        img2 = make_image_clip(images[1], SCENE_DURATION)
        txt2 = make_text_clip(hook, SCENE_DURATION, fontsize=58, position=("center", "center"))
        scene2 = CompositeVideoClip([img2, txt2], size=(VIDEO_W, VIDEO_H))
        scenes.append(scene2)

        # Scene 3 - Captions
        img3 = make_image_clip(images[2], SCENE_DURATION)
        txt3 = make_text_clip(captions, SCENE_DURATION, fontsize=52, position=("center", 0.75))
        scene3 = CompositeVideoClip([img3, txt3], size=(VIDEO_W, VIDEO_H))
        scenes.append(scene3)

        # Scene 4 - Voiceover continues
        img4 = make_image_clip(images[3], SCENE_DURATION)
        scene4 = CompositeVideoClip([img4], size=(VIDEO_W, VIDEO_H))
        scenes.append(scene4)

        # Concatenate with crossfade
        final_video = concatenate_videoclips(scenes, method="compose")

        # Add voiceover audio
        if voiceover_url:
            audio_path = download_file(voiceover_url, ".mp3")
            audio = AudioFileClip(audio_path)
            # Loop or trim audio to match video length
            if audio.duration < final_video.duration:
                audio = audio.fx(afx.audio_loop, duration=final_video.duration)
            else:
                audio = audio.subclip(0, final_video.duration)
            final_video = final_video.set_audio(audio)

        # Export
        output_path = f"/tmp/{video_id}.mp4"
        final_video.write_videofile(
            output_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=f"/tmp/{video_id}_temp.m4a",
            remove_temp=True,
            logger=None
        )

        # Upload to Cloudinary
        result = cloudinary.uploader.upload(
            output_path,
            resource_type="video",
            public_id=f"videos/{video_id}",
            overwrite=True
        )

        return jsonify({
            "success": True,
            "video_url": result["secure_url"],
            "video_id": video_id
        })

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"ERROR: {error_detail}")
        return jsonify({"error": str(e), "detail": error_detail}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
