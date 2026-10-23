"""Read media metadata with Pillow (images) and ffprobe (video)."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import subprocess
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
PARTIAL_EXT = {".part", ".crdownload", ".tmp", ".download", ".partial", ".icloud"}


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_image(path: Path) -> dict:
    with Image.open(path) as im:
        im.verify()  # raises on truncated/corrupt files
    with Image.open(path) as im:
        exif = {}
        try:
            raw = im.getexif()
            for k, v in raw.items():
                name = ExifTags.TAGS.get(k, str(k))
                if name in ("DateTime", "Make", "Model", "Orientation", "Software"):
                    exif[name] = str(v)
            has_gps = 0x8825 in raw
        except Exception:
            has_gps = False
        upright = ImageOps.exif_transpose(im)
        w, h = upright.size
        return {
            "width": w, "height": h, "format": im.format, "mode": im.mode,
            "exif": exif, "has_gps": has_gps,
        }


def probe_video(path: Path) -> dict:
    if not ffmpeg_available():
        raise RuntimeError("ffprobe is not installed; video metadata cannot be read")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout)
    vstreams = [st for st in data.get("streams", []) if st.get("codec_type") == "video"]
    astreams = [st for st in data.get("streams", []) if st.get("codec_type") == "audio"]
    if not vstreams:
        raise RuntimeError("File has no video stream")
    v = vstreams[0]
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = rotation or int((v.get("tags") or {}).get("rotate", 0) or 0)
    if abs(rotation) in (90, 270):
        w, h = h, w
    fps = 0.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except ValueError:
        pass
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    return {
        "width": w, "height": h, "duration": duration, "fps": round(fps, 3),
        "video_codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
        "has_audio": bool(astreams), "audio_codec": astreams[0].get("codec_name") if astreams else None,
        "audio_sample_rate": int(astreams[0].get("sample_rate", 0)) if astreams else None,
        "bit_rate": int(data.get("format", {}).get("bit_rate") or 0), "rotation": rotation,
        "container": data.get("format", {}).get("format_name"),
    }


def probe(path: Path) -> dict:
    kind = media_kind(path)
    info = probe_image(path) if kind == "image" else probe_video(path)
    info["mime"] = mimetypes.guess_type(path.name)[0] or ""
    info["kind"] = kind
    w, h = info.get("width") or 0, info.get("height") or 0
    info["aspect_ratio"] = round(w / h, 4) if h else None
    return info
