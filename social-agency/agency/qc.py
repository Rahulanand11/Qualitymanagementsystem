"""Creative quality control and risk detection. A post is publishable only if no check fails."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from PIL import Image

from .editing import FEED_MAX_RATIO, FEED_MIN_RATIO
from .media_probe import probe_video
from .models import Account, Post

CAPTION_MAX = 2200
HASHTAG_MAX = 30  # Instagram's hard limit
MENTION_MAX = 20

CLAIM_PATTERNS = [
    (r"\bguarantee(d|s)?\b", "guarantee claim"),
    (r"\b(cure[sd]?|heals?|treats?)\b", "health/medical claim"),
    (r"\b(#1|number one|best in the world|world'?s best)\b", "superlative claim"),
    (r"\b100\s?%", "absolute claim"),
    (r"\brisk[- ]free\b", "risk-free claim"),
    (r"\b(lose|losing)\s+\d+\s*(kg|lbs?|pounds)\b", "weight-loss claim"),
    (r"\b(get rich|passive income|financial freedom|returns? of \d+)\b", "financial claim"),
    (r"\bclinically (proven|tested)\b", "clinical claim"),
]
PROMO_PATTERNS = [
    (r"\b\d{1,2}\s?% off\b", "discount"), (r"\b(sale|discount|promo code|coupon|giveaway|contest)\b", "promotion"),
    (r"\bfree shipping\b", "promotion"), (r"\blimited time\b", "promotion"),
]
SENSITIVE_PATTERNS = [
    (r"\b(election|vote|politic\w*|religio\w*)\b", "political/religious topic"),
    (r"\b(diagnos\w+|prescription|medication|therapy|mental health)\b", "health topic"),
    (r"\b(lawsuit|legal advice|attorney)\b", "legal topic"),
]
ENGAGEMENT_BAIT = [r"\b(tag \d+ friends|like if|comment yes|follow for follow|f4f|l4l)\b"]


def scan_text_risks(text: str, banned: list[str]) -> list[str]:
    flags = []
    low = text.lower()
    for pattern, label in CLAIM_PATTERNS + PROMO_PATTERNS + SENSITIVE_PATTERNS:
        if re.search(pattern, low):
            flags.append(label)
    for pattern in ENGAGEMENT_BAIT:
        if re.search(pattern, low):
            flags.append("engagement bait")
    for phrase in banned or []:
        if phrase and phrase.lower() in low:
            flags.append(f"banned phrase: {phrase}")
    return sorted(set(flags))


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail}


def black_frame_ratio(path: str) -> float:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", "blackdetect=d=0.5:pic_th=0.95", "-an",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=1800)
    total = sum(float(d) for d in re.findall(r"black_duration:([\d.]+)", res.stderr))
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", res.stderr)
    seconds = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0
    return total / seconds if seconds else 0.0


def check_media_file(kind: str, path: str, fmt: str) -> list[dict]:
    checks = []
    if not Path(path).exists():
        return [_check("file_exists", "fail", "Exported file is missing")]
    if kind == "image" or path.endswith(".jpg"):
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                w, h = im.size
                fmt_ok = im.format == "JPEG"
        except Exception as exc:
            return [_check("file_integrity", "fail", f"Corrupt image: {exc}")]
        checks.append(_check("file_integrity", "pass"))
        checks.append(_check("jpeg_format", "pass" if fmt_ok else "fail", "Instagram API image publishing requires JPEG"))
        r = w / h
        ok = FEED_MIN_RATIO - 0.005 <= r <= FEED_MAX_RATIO + 0.005 or fmt == "story"
        checks.append(_check("aspect_ratio", "pass" if ok else "fail", f"{w}x{h} ({r:.3f}); feed accepts 0.8-1.91"))
        checks.append(_check("resolution", "pass" if w >= 1080 else "warn", f"width {w}px (1080px recommended)"))
    else:
        try:
            info = probe_video(Path(path))
        except Exception as exc:
            return [_check("file_integrity", "fail", f"Unreadable video: {exc}")]
        checks.append(_check("file_integrity", "pass"))
        checks.append(_check("codec", "pass" if info["video_codec"] == "h264" and info.get("audio_codec") == "aac" else "fail",
                             f"{info['video_codec']}/{info.get('audio_codec')}"))
        checks.append(_check("resolution", "pass" if (info["width"], info["height"]) == (1080, 1920) else "warn",
                             f"{info['width']}x{info['height']}"))
        d = info["duration"]
        checks.append(_check("duration", "pass" if 3 <= d <= 900 else "fail", f"{d:.1f}s (Reels: 3s-15min)"))
        ratio = black_frame_ratio(path)
        checks.append(_check("black_frames", "pass" if ratio < 0.05 else ("warn" if ratio < 0.2 else "fail"),
                             f"{ratio:.0%} of runtime is black"))
    return checks


def run_post_qc(post: Post, account: Account) -> dict:
    """Full pre-publish QC. Returns {"score": int, "checks": [...], "risk_flags": [...], "blocking": bool}."""
    brand = account.brand
    checks: list[dict] = []
    checks.append(_check("account_assigned", "pass" if post.account_id == account.id else "fail"))
    items = list(post.media)
    if not items:
        checks.append(_check("media_present", "fail", "Post has no media"))
    if post.format == "carousel" and not 2 <= len(items) <= 10:
        checks.append(_check("carousel_size", "fail", f"{len(items)} items (2-10 allowed)"))
    for pm in items:
        if pm.asset.account_id != account.id:
            checks.append(_check("asset_account", "fail", f"Asset #{pm.asset_id} belongs to another account"))
        if pm.derivative is None:
            checks.append(_check("export_present", "fail", f"Asset #{pm.asset_id} has no export"))
            continue
        for c in check_media_file(pm.asset.media_kind if post.format != "reel" else "video", pm.derivative.path, post.format):
            c["detail"] = f"asset #{pm.asset_id}: {c['detail']}"
            checks.append(c)
        if not pm.asset.rights_confirmed:
            checks.append(_check("content_rights", "warn", f"Rights not confirmed for asset #{pm.asset_id}"))
        sub = (pm.derivative.qc or {}).get("subtitles", "")
        if post.format == "reel" and (brand.automation_rules or {}).get("require_subtitles") and sub != "burned_in":
            checks.append(_check("subtitles", "warn", f"Subtitles required by account rules: {sub}"))
        ai = (pm.asset.analysis or {}).get("ai") or {}
        if ai.get("has_text_or_watermark") and "watermark" in (ai.get("watermark_notes") or "").lower():
            checks.append(_check("watermark", "warn", ai.get("watermark_notes", "")))

    caption = post.caption or ""
    tags = post.hashtags or []
    full = caption + " " + " ".join(f"#{t}" for t in tags)
    checks.append(_check("caption_present", "pass" if caption.strip() else "fail"))
    checks.append(_check("caption_length", "pass" if len(full) <= CAPTION_MAX else "fail", f"{len(full)}/{CAPTION_MAX} chars"))
    checks.append(_check("hashtag_count", "pass" if len(tags) <= HASHTAG_MAX else "fail", f"{len(tags)} hashtags"))
    if len(tags) > 12:
        checks.append(_check("hashtag_relevance", "warn", "More than 12 hashtags tends to read as spam"))
    mentions = len(re.findall(r"@\w+", caption))
    checks.append(_check("mention_count", "pass" if mentions <= MENTION_MAX else "fail", f"{mentions} mentions"))
    dup = [t for t in tags if tags.count(t) > 1]
    if dup:
        checks.append(_check("duplicate_hashtags", "warn", ", ".join(sorted(set(dup)))))

    risk = scan_text_risks(full + " " + (post.on_screen_text or ""), brand.banned_phrases if brand else [])
    for pm in items:
        for f in ((pm.asset.analysis or {}).get("ai") or {}).get("sensitive_flags") or []:
            risk.append(f"visual: {f}")
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    score = max(0, 100 - 30 * fails - 7 * warns - 5 * len(risk))
    return {"score": score, "checks": checks, "risk_flags": sorted(set(risk)), "blocking": fails > 0}
