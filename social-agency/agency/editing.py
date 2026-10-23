"""Minimal-necessary media editing.

Images: measured corrections only where the statistics show a problem (levels, exposure,
white balance, saturation, sharpening), an optional account preset, and crops to Instagram
ratios. No geometric warping, face or body changes, object invention, or clothing edits.
GPS/EXIF metadata is stripped from exports for privacy.

Videos: trim dead leading/trailing time, fit to 1080x1920 (blurred fill rather than an
aggressive crop when the source is landscape), loudness normalisation, H.264/AAC MP4 with
faststart, cover frame selection, optional subtitles when a speech-to-text engine is installed.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .analysis import image_stats, load_rgb
from .config import get_settings
from .models import Asset, Derivative

log = logging.getLogger("agency.editing")

# Instagram feed accepts aspect ratios between 4:5 (0.8) and 1.91:1.
FEED_MIN_RATIO, FEED_MAX_RATIO = 0.8, 1.91
FEED_WIDTH = 1080
REEL_W, REEL_H = 1080, 1920

PRESETS = {
    "natural": {},
    "warm": {"temperature": 0.03, "saturation": 1.04},
    "cool": {"temperature": -0.03},
    "vibrant": {"saturation": 1.12, "contrast": 1.04},
    "matte": {"contrast": 0.94, "lift_blacks": 0.04},
    "mono": {"saturation": 0.0, "contrast": 1.06},
}


def asset_dir(asset: Asset) -> Path:
    d = get_settings().storage_dir / f"asset_{asset.id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                      "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def font_path() -> str | None:
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(candidate).exists():
            return candidate
    return None


# ================================================================== images
def enhance_image(im: Image.Image, preset: str = "natural") -> tuple[Image.Image, list[dict], dict]:
    """Apply only the corrections the measurements call for. Returns (image, operations, stats_before)."""
    before = image_stats(im)
    ops: list[dict] = []
    arr = np.asarray(im, dtype=np.float32) / 255.0

    # 1. Levels: stretch only when the tonal range is clearly compressed.
    lo, hi = before["p1"], before["p99"]
    if lo > 0.06 or hi < 0.92:
        # Move each end 60% of the way to its target instead of a full stretch, to keep the look natural.
        new_lo = lo if lo <= 0.06 else lo * 0.4
        new_hi = hi if hi >= 0.92 else hi + (0.97 - hi) * 0.6
        arr = np.clip((arr - lo) / max(hi - lo, 1e-3) * (new_hi - new_lo) + new_lo, 0, 1)
        ops.append({"op": "levels", "black_point": round(lo, 3), "white_point": round(hi, 3),
                    "reason": "compressed tonal range"})

    # 2. Exposure: gentle gamma towards a mid-grey mean.
    luma = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]).mean()
    if luma < 0.38 or luma > 0.68:
        target = 0.45 if luma < 0.38 else 0.58
        gamma = float(np.clip(np.log(target) / np.log(max(luma, 1e-3)), 0.6, 1.5))
        arr = np.clip(arr ** gamma, 0, 1)
        ops.append({"op": "exposure_gamma", "gamma": round(gamma, 3),
                    "reason": "underexposed" if luma < 0.38 else "overexposed"})

    # 3. White balance: half-strength grey-world correction when a cast is measurable.
    means = arr.reshape(-1, 3).mean(axis=0)
    cast = means - means.mean()
    if np.abs(cast).max() > 0.035:
        gains = 1 - 0.5 * cast / np.maximum(means, 1e-3)
        arr = np.clip(arr * gains, 0, 1)
        ops.append({"op": "white_balance", "gains": [round(float(g), 3) for g in gains], "reason": "colour cast"})

    # 4. Account preset (brand colour grade).
    p = PRESETS.get(preset, {})
    if p.get("temperature"):
        t = p["temperature"]
        arr = np.clip(arr + np.array([t, 0, -t], dtype=np.float32), 0, 1)
    if p.get("lift_blacks"):
        arr = arr * (1 - p["lift_blacks"]) + p["lift_blacks"]
    out = Image.fromarray((arr * 255).round().astype(np.uint8), "RGB")
    if p:
        if "contrast" in p:
            out = ImageEnhance.Contrast(out).enhance(p["contrast"])
        if "saturation" in p:
            out = ImageEnhance.Color(out).enhance(p["saturation"])
        ops.append({"op": "preset", "name": preset, "params": p})

    # 5. Saturation: only for clearly dull images (and not if a preset already set it).
    if before["colorfulness"] < 0.12 and "saturation" not in p:
        out = ImageEnhance.Color(out).enhance(1.10)
        ops.append({"op": "saturation", "factor": 1.10, "reason": "muted colour"})

    # 6. Sharpening: mild unsharp mask for slightly soft images.
    if before["sharpness"] < 3.0:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.6, percent=60, threshold=3))
        ops.append({"op": "unsharp_mask", "radius": 1.6, "percent": 60, "reason": "soft detail"})
    return out, ops, before


def saliency_center(im: Image.Image) -> tuple[float, float]:
    """Centre of visual interest from edge energy (keeps the subject in frame when cropping)."""
    small = im.copy()
    small.thumbnail((256, 256))
    g = np.asarray(small.convert("L"), dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1))[:-1, :]
    gy = np.abs(np.diff(g, axis=0))[:, :-1]
    energy = gx + gy
    if energy.sum() <= 0:
        return 0.5, 0.5
    ys, xs = np.mgrid[0:energy.shape[0], 0:energy.shape[1]]
    cx = float((xs * energy).sum() / energy.sum() / energy.shape[1])
    cy = float((ys * energy).sum() / energy.sum() / energy.shape[0])
    # Bias toward the centre so crops are not pulled to a busy edge.
    return 0.5 + (cx - 0.5) * 0.6, 0.5 + (cy - 0.5) * 0.6


def crop_to_ratio(im: Image.Image, ratio: float) -> tuple[Image.Image, dict | None]:
    w, h = im.size
    current = w / h
    if abs(current - ratio) < 0.01:
        return im, None
    cx, cy = saliency_center(im)
    if current > ratio:  # too wide: crop width
        nw = int(round(h * ratio))
        left = int(np.clip(cx * w - nw / 2, 0, w - nw))
        box = (left, 0, left + nw, h)
    else:  # too tall: crop height
        nh = int(round(w / ratio))
        top = int(np.clip(cy * h - nh / 2, 0, h - nh))
        box = (0, top, w, top + nh)
    return im.crop(box), {"op": "crop", "box": list(box), "ratio": round(ratio, 4)}


def feed_ratio_for(width: int, height: int) -> float:
    r = width / height
    if FEED_MIN_RATIO <= r <= FEED_MAX_RATIO:
        return r  # already valid: no crop (minimum necessary editing)
    return FEED_MIN_RATIO if r < FEED_MIN_RATIO else FEED_MAX_RATIO


def _save_jpeg(im: Image.Image, path: Path) -> None:
    # Saving without exif drops GPS and device metadata.
    im.save(path, "JPEG", quality=92, optimize=True, progressive=True, subsampling=0)


def _resize_width(im: Image.Image, width: int) -> Image.Image:
    if im.width <= width:
        return im
    return im.resize((width, int(round(im.height * width / im.width))), Image.LANCZOS)


def add_text_overlay(im: Image.Image, text: str, brand_color: str = "#FFFFFF") -> Image.Image:
    """Title text on a soft gradient band at the bottom (used for covers and title slides)."""
    out = im.copy().convert("RGBA")
    w, h = out.size
    band = Image.new("RGBA", (w, h // 3), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    for y in range(band.height):
        bd.line([(0, y), (w, y)], fill=(0, 0, 0, int(170 * y / band.height)))
    out.alpha_composite(band, (0, h - band.height))
    draw = ImageDraw.Draw(out)
    size = max(28, w // 14)
    font = _font(size)
    words, lines, line = text.split(), [], ""
    for word in words:
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=font) > w * 0.86 and line:
            lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)
    y = h - int(size * 1.35) * len(lines) - h // 18
    for ln in lines[:3]:
        tw = draw.textlength(ln, font=font)
        draw.text(((w - tw) / 2, y), ln, font=font, fill=brand_color, stroke_width=max(1, size // 18),
                  stroke_fill=(0, 0, 0))
        y += int(size * 1.35)
    return out.convert("RGB")


def process_image(asset: Asset, preset: str = "natural") -> list[Derivative]:
    out_dir = asset_dir(asset)
    im = load_rgb(asset.original_path)
    enhanced, ops, before = enhance_image(im, preset)
    derivs: list[Derivative] = []

    ratio = feed_ratio_for(*enhanced.size)
    feed, crop_op = crop_to_ratio(enhanced, ratio)
    feed = _resize_width(feed, FEED_WIDTH)
    feed_ops = ops + ([crop_op] if crop_op else []) + [{"op": "resize", "width": feed.width}, {"op": "strip_metadata"}]
    p = out_dir / "feed.jpg"
    _save_jpeg(feed, p)
    derivs.append(Derivative(asset_id=asset.id, kind="feed", path=str(p), width=feed.width, height=feed.height,
                             operations=feed_ops, qc={"stats_before": before, "stats_after": image_stats(feed)}))

    # 4:5 portrait version for consistent carousels (Instagram crops all carousel items to the first ratio).
    portrait, c2 = crop_to_ratio(enhanced, 0.8)
    portrait = _resize_width(portrait, FEED_WIDTH)
    p = out_dir / "portrait_4x5.jpg"
    _save_jpeg(portrait, p)
    derivs.append(Derivative(asset_id=asset.id, kind="portrait", path=str(p), width=portrait.width,
                             height=portrait.height, operations=ops + ([c2] if c2 else []) + [{"op": "strip_metadata"}]))

    story, c3 = crop_to_ratio(enhanced, 9 / 16)
    story = _resize_width(story, REEL_W)
    p = out_dir / "story_9x16.jpg"
    _save_jpeg(story, p)
    derivs.append(Derivative(asset_id=asset.id, kind="story", path=str(p), width=story.width, height=story.height,
                             operations=ops + ([c3] if c3 else []) + [{"op": "strip_metadata"}]))

    preview = feed.copy()
    preview.thumbnail((480, 480))
    p = out_dir / "preview.jpg"
    _save_jpeg(preview, p)
    derivs.append(Derivative(asset_id=asset.id, kind="preview", path=str(p), width=preview.width,
                             height=preview.height, operations=[{"op": "thumbnail"}]))
    return derivs


# ================================================================== videos
def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[-600:]}")
    return res


def detect_dead_edges(path: str, duration: float) -> tuple[float, float, dict]:
    """Leading/trailing black frames or silence to trim. Conservative: never trims more than 25%."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", "blackdetect=d=0.3:pic_th=0.95",
                          "-af", "silencedetect=n=-45dB:d=0.6", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=1800)
    log_text = res.stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log_text)]
    sil_starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log_text)]
    sil_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log_text)]
    silences = list(zip(sil_starts, sil_ends + [duration] * (len(sil_starts) - len(sil_ends))))
    start, end = 0.0, duration
    for a, b in blacks:
        if a <= 0.05:
            start = max(start, b)
        if b >= duration - 0.1:
            end = min(end, a)
    for a, b in silences:  # leading silence only matters together with a static/black opening
        if a <= 0.05 and b - a > 1.5 and start > 0:
            start = max(start, min(b, start + 1.0))
    max_trim = duration * 0.25
    start = min(start, max_trim)
    end = max(end, duration - max_trim)
    return round(start, 2), round(end, 2), {"black_segments": blacks, "silences": silences}


def transcribe(path: str) -> list[dict] | None:
    """Optional speech-to-text. Returns segments [{start, end, text}] or None if unavailable."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return None
    model = WhisperModel("small", compute_type="int8")
    segments, _ = model.transcribe(path, vad_filter=True)
    return [{"start": seg.start, "end": seg.end, "text": seg.text.strip()} for seg in segments]


def write_srt(segments: list[dict], path: Path) -> None:
    def ts(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(sec):02},{int((sec % 1) * 1000):03}"
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{ts(seg['start'])} --> {ts(seg['end'])}", seg["text"], ""]
    path.write_text("\n".join(lines))


def process_video(asset: Asset, title: str = "", want_subtitles: bool = True) -> list[Derivative]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    out_dir = asset_dir(asset)
    src = asset.original_path
    duration = float(asset.duration or 0)
    has_audio = bool((asset.meta or {}).get("has_audio"))
    ops: list[dict] = []

    start, end, detect = detect_dead_edges(src, duration)
    if start > 0.05 or end < duration - 0.05:
        ops.append({"op": "trim", "start": start, "end": end, "reason": "dead time at start/end", **detect})
    length = max(end - start, 0.5)

    ratio = (asset.width or 1) / (asset.height or 1)
    if abs(ratio - 9 / 16) < 0.02:
        vf = f"scale={REEL_W}:{REEL_H}:flags=lanczos,setsar=1"
        ops.append({"op": "scale", "size": f"{REEL_W}x{REEL_H}"})
    elif ratio < 9 / 16 + 0.15:  # near-vertical: crop the small excess
        vf = (f"scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={REEL_W}:{REEL_H},setsar=1")
        ops.append({"op": "fill_crop", "size": f"{REEL_W}x{REEL_H}"})
    else:  # landscape/square: keep the full frame over a blurred fill
        vf = (f"split[a][b];[a]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase,crop={REEL_W}:{REEL_H},"
              f"boxblur=24:2,eq=brightness=-0.08[bg];[b]scale={REEL_W}:-2:flags=lanczos[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
        ops.append({"op": "blurred_fill", "size": f"{REEL_W}x{REEL_H}", "reason": "preserve full frame"})

    fp = font_path()
    if title and fp:
        safe = title.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")[:60]
        vf += (f",drawtext=fontfile={fp}:text='{safe}':fontcolor=white:fontsize=64:borderw=4:bordercolor=black@0.7:"
               f"x=(w-text_w)/2:y=h*0.12:enable='lt(t,2.5)'")
        ops.append({"op": "title_overlay", "text": title, "seconds": 2.5})

    subtitles_status = "not_requested"
    srt_path = out_dir / "subtitles.srt"
    if want_subtitles and has_audio:
        segs = transcribe(src)
        if segs is None:
            subtitles_status = "unavailable: install faster-whisper to enable automatic subtitles"
        elif segs:
            shifted = [{"start": max(0, x["start"] - start), "end": max(0, x["end"] - start), "text": x["text"]}
                       for x in segs if x["end"] > start and x["start"] < end]
            write_srt(shifted, srt_path)
            vf += f",subtitles={srt_path}:force_style='FontSize=14,Outline=2,MarginV=60'"
            subtitles_status = "burned_in"
            ops.append({"op": "subtitles", "segments": len(shifted)})
        else:
            subtitles_status = "no_speech_detected"

    out = out_dir / "reel.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start}", "-t", f"{length:.2f}", "-i", src]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-filter_complex", f"[0:v]{vf},fps=30,format=yuv420p[v]", "-map", "[v]"]
    if has_audio:
        cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-map", "0:a:0"]
        ops.append({"op": "loudnorm", "target_lufs": -14})
    else:
        cmd += ["-map", "1:a:0"]
        ops.append({"op": "silent_audio_track", "reason": "source has no audio; Instagram expects an audio stream"})
    cmd += ["-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "20", "-maxrate", "8M",
            "-bufsize", "16M", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-shortest", str(out)]
    _run(cmd)
    ops.append({"op": "encode", "codec": "h264/aac", "fps": 30})

    derivs = [Derivative(asset_id=asset.id, kind="reel", path=str(out), width=REEL_W, height=REEL_H,
                         duration=round(length, 2), operations=ops, qc={"subtitles": subtitles_status})]
    derivs += make_cover(asset, out, title)
    return derivs


def make_cover(asset: Asset, video_path: Path, title: str = "") -> list[Derivative]:
    """Pick the sharpest, well-exposed frame of the exported reel as the cover."""
    out_dir = asset_dir(asset)
    dur = probe_duration(video_path)
    best, best_score = None, -1.0
    for i, frac in enumerate((0.1, 0.25, 0.4, 0.55, 0.7)):
        target = out_dir / f"cover_candidate_{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{dur * frac:.2f}", "-i", str(video_path),
                        "-frames:v", "1", "-q:v", "2", str(target)], capture_output=True, timeout=120)
        if not target.exists():
            continue
        st = image_stats(load_rgb(target, 512))
        score = st["sharpness"] * (1 - abs(st["brightness"] - 0.5)) * (0.5 + st["colorfulness"])
        if score > best_score:
            best, best_score = (frac, target), score
    if best is None:
        return []
    im = load_rgb(best[1])
    ops = [{"op": "cover_frame", "at_fraction": best[0]}]
    if title:
        im = add_text_overlay(im, title)
        ops.append({"op": "cover_title", "text": title})
    cover = out_dir / "cover.jpg"
    _save_jpeg(im, cover)
    prev = im.copy()
    prev.thumbnail((480, 480))
    prev_path = out_dir / "preview.jpg"
    _save_jpeg(prev, prev_path)
    return [
        Derivative(asset_id=asset.id, kind="cover", path=str(cover), width=im.width, height=im.height, operations=ops),
        Derivative(asset_id=asset.id, kind="preview", path=str(prev_path), width=prev.width, height=prev.height,
                   operations=[{"op": "thumbnail"}]),
    ]


def probe_duration(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(json.loads(res.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def concat_reels(paths: list[str], out: Path) -> float:
    """Join already-normalised reel segments (same codec/size) into one reel."""
    list_file = out.with_suffix(".txt")
    list_file.write_text("".join(f"file '{p}'\n" for p in paths))
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
          "-c", "copy", "-movflags", "+faststart", str(out)])
    return probe_duration(out)
