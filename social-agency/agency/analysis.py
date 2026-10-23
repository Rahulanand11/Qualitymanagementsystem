"""Asset analysis: objective image statistics plus Claude vision analysis (when available)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai, image_block
from .config import get_settings
from .models import Account, Asset

log = logging.getLogger("agency.analysis")


# ------------------------------------------------------------ measured stats
def load_rgb(path: str | Path, max_side: int | None = None) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max_side:
            im.thumbnail((max_side, max_side))
        return im.copy()


def image_stats(im: Image.Image) -> dict:
    """Objective measurements used to decide which edits are actually needed."""
    small = im.copy()
    small.thumbnail((512, 512))
    arr = np.asarray(small, dtype=np.float32) / 255.0
    luma = 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorfulness = float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
    gray = luma
    lap = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:])
    means = arr.reshape(-1, 3).mean(axis=0)
    cast = means - means.mean()
    return {
        "brightness": round(float(luma.mean()), 4),
        "p1": round(float(np.percentile(luma, 1)), 4),
        "p99": round(float(np.percentile(luma, 99)), 4),
        "contrast": round(float(luma.std()), 4),
        "colorfulness": round(colorfulness, 4),
        "sharpness": round(float(lap.var() * 1000), 4),
        "clipped_highlights": round(float((luma > 0.98).mean()), 4),
        "crushed_shadows": round(float((luma < 0.02).mean()), 4),
        "color_cast": [round(float(c), 4) for c in cast],
    }


def heuristic_quality(stats: dict) -> tuple[int, list[str]]:
    issues = []
    score = 100
    if stats["brightness"] < 0.25:
        issues.append("underexposed"); score -= 20
    elif stats["brightness"] > 0.8:
        issues.append("overexposed"); score -= 20
    if stats["contrast"] < 0.12:
        issues.append("flat / low contrast"); score -= 10
    if stats["sharpness"] < 0.8:
        issues.append("soft or blurry"); score -= 25
    if stats["clipped_highlights"] > 0.08:
        issues.append("clipped highlights"); score -= 10
    if max(abs(c) for c in stats["color_cast"]) > 0.06:
        issues.append("colour cast"); score -= 5
    return max(score, 0), issues


# --------------------------------------------------------------- AI schema
class AssetAnalysis(BaseModel):
    summary: str = Field(description="One or two sentences describing what the media shows")
    subjects: list[str]
    setting: str
    people_present: bool
    people_count_estimate: int
    products_visible: list[str] = Field(description="Products or brands visibly shown; empty if none")
    mood: str
    visual_quality: int = Field(description="0-100 technical and aesthetic quality for Instagram")
    quality_issues: list[str]
    has_text_or_watermark: bool
    watermark_notes: str
    suggested_pillar: str = Field(description="Best matching content pillar name from the provided list, or a short new label")
    suggested_formats: list[Literal["image", "carousel", "reel", "story"]]
    instagram_suitability: int = Field(description="0-100: how suitable this is to publish for this account")
    suitability_notes: str
    sensitive_flags: list[str] = Field(description="e.g. minors, private_information, nudity, medical, alcohol, weapons, political, third_party_brand, license_plate; empty if none")
    best_frame_index: int = Field(description="For videos: index of the strongest frame among those provided (0 for images)")
    strong_opening: str = Field(description="For videos: the most engaging moment to open with; empty for images")
    recommended_edits: list[str]


ANALYSIS_SYSTEM = (
    "You are the creative director of a premium social media agency reviewing raw uploads for an Instagram account. "
    "Describe only what is visible. Do not guess identities of people. Flag anything that would need human approval "
    "before publishing (people who may not have consented, minors, private information like addresses or screens, "
    "third-party logos, sensitive topics). Be honest about quality problems."
)


def extract_frames(asset: Asset, count: int = 5) -> list[tuple[float, Path]]:
    """Grab evenly spaced frames from a video for analysis and cover selection."""
    out_dir = get_settings().storage_dir / f"asset_{asset.id}" / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = asset.duration or 0
    frames = []
    for i in range(count):
        t = max(0.0, duration * (i + 0.5) / count)
        target = out_dir / f"frame_{i}.jpg"
        if not target.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", asset.original_path,
                            "-frames:v", "1", "-q:v", "3", str(target)], capture_output=True, timeout=120)
        if target.exists():
            frames.append((round(t, 2), target))
    return frames


def analyze_asset(s: Session, asset: Asset) -> dict:
    account: Account | None = s.get(Account, asset.account_id) if asset.account_id else None
    frames: list[tuple[float, Path]] = []
    if asset.media_kind == "image":
        im = load_rgb(asset.original_path, 1024)
        stats = image_stats(im)
        image_paths = [Path(asset.original_path)]
    else:
        frames = extract_frames(asset)
        if not frames:
            raise RuntimeError("Could not extract frames from video")
        stats_list = [image_stats(load_rgb(p, 512)) for _, p in frames]
        stats = {k: round(float(np.mean([st[k] for st in stats_list])), 4) for k in stats_list[0] if k != "color_cast"}
        stats["color_cast"] = stats_list[0]["color_cast"]
        stats["frame_sharpness"] = [st["sharpness"] for st in stats_list]
        stats["frame_brightness"] = [st["brightness"] for st in stats_list]
        image_paths = [p for _, p in frames]

    h_score, h_issues = heuristic_quality(stats)
    result = {"stats": stats, "heuristic_quality": h_score, "heuristic_issues": h_issues,
              "frames": [{"t": t, "path": str(p)} for t, p in frames]}

    brand = account.brand if account else None
    pillars = [p.get("name") for p in (brand.content_pillars if brand else []) if p.get("name")]
    context = (
        f"Account: {account.name if account else 'unassigned'} ({account.account_type if account else '-'})\n"
        f"Niche: {brand.niche if brand else ''}\nPositioning: {brand.positioning if brand else ''}\n"
        f"Content pillars: {', '.join(pillars) or 'not defined yet'}\n"
        f"Media: {asset.media_kind}, {asset.width}x{asset.height}"
        + (f", {asset.duration:.1f}s, frames at {[t for t, _ in frames]}" if asset.media_kind == "video" else "")
        + f"\nCampaign folder: {asset.campaign or '-'}\nMeasured stats: {stats}"
    )
    try:
        blocks = [image_block(str(p)) for p in image_paths] + [{"type": "text", "text": context}]
        parsed, source = get_ai().structured(s, workflow="asset_analysis", schema=AssetAnalysis,
                                             system=ANALYSIS_SYSTEM, content=blocks, account_id=asset.account_id)
        result["ai"] = parsed.model_dump()
        asset.analysis_source = source
    except AIUnavailable as exc:
        result["ai"] = None
        result["ai_unavailable_reason"] = str(exc)
        asset.analysis_source = "heuristic"
    asset.analysis = result
    return result


def suitability(asset: Asset) -> tuple[int, list[str]]:
    """Combined quality score and reasons this asset needs a human look."""
    a = asset.analysis or {}
    reasons = []
    ai = a.get("ai")
    score = a.get("heuristic_quality", 0)
    if ai:
        score = round(0.4 * score + 0.6 * min(ai.get("visual_quality", 0), ai.get("instagram_suitability", 100)))
        if ai.get("sensitive_flags"):
            reasons.append("Sensitive content flagged: " + ", ".join(ai["sensitive_flags"]))
        if ai.get("has_text_or_watermark") and "watermark" in (ai.get("watermark_notes") or "").lower():
            reasons.append("Possible watermark: " + ai["watermark_notes"])
    else:
        reasons.append("AI visual analysis unavailable; content was not reviewed for people, rights or sensitive material")
    return score, reasons
