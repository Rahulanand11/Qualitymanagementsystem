import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from tests.fake_graph import FakeGraph


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENCY_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agency.example.com")
    monkeypatch.setenv("META_APP_ID", "123")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-xyz")
    monkeypatch.setenv("UPLOAD_STABLE_SECONDS", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from agency import ai, config, db, instagram

    config.reset_settings()
    db.reset_engine()
    db.init_db()
    graph = FakeGraph()
    monkeypatch.setattr(instagram, "transport_factory", graph.transport)
    ai.set_ai(None)
    yield SimpleNamespace(tmp=tmp_path, graph=graph, root=config.get_settings().watch_root)
    ai.set_ai(None)
    db.reset_engine()
    config.reset_settings()


def make_image(path: Path, size=(1600, 1200), dark=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    h, w = size[1], size[0]
    y, x = np.mgrid[0:h, 0:w]
    arr = np.stack([(x / w * 200 + 30), (y / h * 180 + 40), np.full((h, w), 120.0)], axis=-1)
    arr += rng.normal(0, 8, arr.shape)
    if dark:
        arr *= 0.35
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path, "JPEG", quality=90)
    return path


def make_video(path: Path, seconds=4, size="1280x720", audio=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=black:s={size}:d=0.6",
           "-f", "lavfi", "-i", f"testsrc=s={size}:d={seconds}:r=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds + 0.6}"]
    cmd += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        cmd += ["-map", "2:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


class FakeAnthropic:
    """Mimics client.beta.messages.parse; builds a valid instance of the requested schema."""

    def __init__(self, factories: dict):
        self.factories = factories
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        schema = kw["output_format"]
        parsed = schema.model_validate(self.factories[schema.__name__](kw))
        return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", model=kw["model"],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=400))


def analysis_payload(_kw):
    return {"summary": "A sunlit city street with cafe tables.", "subjects": ["street", "cafe"], "setting": "city",
            "people_present": False, "people_count_estimate": 0, "products_visible": [], "mood": "relaxed",
            "visual_quality": 88, "quality_issues": [], "has_text_or_watermark": False, "watermark_notes": "",
            "suggested_pillar": "City Guides", "suggested_formats": ["image", "carousel"],
            "instagram_suitability": 90, "suitability_notes": "", "sensitive_flags": [], "best_frame_index": 0,
            "strong_opening": "", "recommended_edits": []}


def copy_payload(_kw):
    angle = {"angle_name": "guide", "rationale": "saves", "hook": "Three cafes worth the detour in Lisbon",
             "caption": "Three cafes worth the detour in Lisbon\n\nSlow mornings, good light, better coffee.",
             "cta": "Save this for your next trip.", "hashtags": ["lisbon", "#cafeguide", "travelphotography"],
             "keywords": ["lisbon cafes", "lisbon travel guide"], "on_screen_text": "", "alt_text": "Cafe tables on a sunlit Lisbon street",
             "cover_title": "Lisbon cafes"}
    return {"pillar": "City Guides", "objective": "reach", "target_audience": "travellers 25-34",
            "concept": "Mini guide", "angles": [angle, {**angle, "angle_name": "story", "hook": "I almost skipped this street"}],
            "selected_index": 0, "selection_reason": "Guides get saved", "claims_needing_verification": [],
            "spelling_grammar_ok": True}


@pytest.fixture()
def fake_ai(env, monkeypatch):
    from agency import ai

    client = FakeAnthropic({"AssetAnalysis": analysis_payload, "PostCopy": copy_payload,
                            "ReportNarrative": lambda kw: {"summary": "Solid week.", "problems": [], "next_actions": ["Post 2 reels"]},
                            "MonthlyStrategy": lambda kw: {"campaign_themes": ["Lisbon"], "weekly_focus": ["a"], "key_dates": [],
                                                           "pillar_allocation": {"City Guides": 100}, "format_mix": {"reel": 8},
                                                           "brand_vs_conversion": "80/20", "repurposing": [], "production_needs": [],
                                                           "growth_targets": ["beat median reach"], "testing_priorities": [],
                                                           "rationale": "data"},
                            "AuditNarrative": lambda kw: {"executive_summary": "ok", "opportunities": [], "weaknesses": [],
                                                          "best_categories": [], "content_gaps": [], "roadmap_30_days": []},
                            "TriageBatch": lambda kw: {"items": []}})
    ai.set_ai(ai.AIService(client=client))
    return client
