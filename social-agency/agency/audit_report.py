"""Profile audit with a transparent health score, opportunities, weaknesses and a 30-day roadmap."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai
from .analytics import media_rows
from .growth import performance_summary
from .models import Account, Report, utcnow

WEIGHTS = {"profile": 20, "consistency": 20, "engagement_quality": 20, "momentum": 20, "format_mix": 20}


def _component(name: str, score: float | None, evidence: dict, method: str) -> dict:
    return {"component": name, "weight": WEIGHTS[name], "score": None if score is None else round(score),
            "evidence": evidence, "method": method}


def health_score(s: Session, account: Account) -> dict:
    p = account.profile_cache or {}
    b = account.brand
    rows = [r for r in media_rows(s, account, utcnow() - dt.timedelta(days=120)) if r["posted_at"]]
    comps = []

    # Profile completeness
    if p:
        bio = p.get("biography") or ""
        kw = [w.lower() for w in (b.niche or "").replace(",", " ").split() if len(w) > 3]
        checks = {"has_bio": bool(bio), "bio_length_ok": 40 <= len(bio) <= 150, "has_link": bool(p.get("website")),
                  "has_name": bool(p.get("name")), "has_profile_picture": bool(p.get("profile_picture_url")),
                  "niche_keyword_in_name_or_bio": any(k in (bio + " " + (p.get("name") or "")).lower() for k in kw) if kw else None}
        scored = [v for v in checks.values() if v is not None]
        comps.append(_component("profile", 100 * sum(scored) / len(scored), checks,
                                "Share of profile checks passed (bio, length, link, name, picture, niche keyword)."))
    else:
        comps.append(_component("profile", None, {}, "Requires a connected, synced account."))

    # Posting consistency (last 8 weeks)
    weeks = Counter(dt.datetime.fromisoformat(r["posted_at"]).isocalendar()[1] for r in rows
                    if dt.datetime.fromisoformat(r["posted_at"]) > utcnow() - dt.timedelta(weeks=8))
    if rows:
        active = sum(1 for c in weeks.values() if c >= 1)
        avg = sum(weeks.values()) / 8
        score = 60 * active / 8 + 40 * min(avg / max(account.posts_per_week, 1), 1)
        comps.append(_component("consistency", score, {"weeks_with_posts_of_8": active, "avg_posts_per_week": round(avg, 2),
                                                       "target_per_week": account.posts_per_week},
                                "60% weeks with at least one post + 40% average weekly volume vs. target."))
    else:
        comps.append(_component("consistency", None, {}, "No synced posts."))

    # Engagement quality: share of interactions that are saves + shares (signals of value)
    sv = [(r["metrics"].get("saved") or 0) + (r["metrics"].get("shares") or 0) for r in rows if r["interactions"]]
    tot = [r["interactions"] for r in rows if r["interactions"]]
    if tot:
        share = sum(sv) / sum(tot)
        comps.append(_component("engagement_quality", min(share / 0.25, 1) * 100,
                                {"saves_shares_share_of_interactions": round(share, 3), "posts": len(tot)},
                                "Saves+shares as a share of interactions; 25% or more scores 100."))
    else:
        comps.append(_component("engagement_quality", None, {}, "No interaction data yet."))

    # Momentum: median reach last 30 days vs. prior 60 days
    now = utcnow()
    recent = [r["metrics"].get("reach") for r in rows if r["metrics"].get("reach") is not None
              and dt.datetime.fromisoformat(r["posted_at"]) > now - dt.timedelta(days=30)]
    prior = [r["metrics"].get("reach") for r in rows if r["metrics"].get("reach") is not None
             and now - dt.timedelta(days=90) < dt.datetime.fromisoformat(r["posted_at"]) <= now - dt.timedelta(days=30)]
    if len(recent) >= 2 and len(prior) >= 2:
        ratio = statistics.median(recent) / max(statistics.median(prior), 1)
        comps.append(_component("momentum", max(0, min(100, 50 * ratio)),
                                {"median_reach_30d": statistics.median(recent), "median_reach_prior_60d": statistics.median(prior)},
                                "50 x (recent median reach / prior median reach), capped at 100. 50 = flat."))
    else:
        comps.append(_component("momentum", None, {"recent_posts": len(recent), "prior_posts": len(prior)},
                                "Needs 2+ posts with reach in each window."))

    # Format mix
    fmts = Counter(r["format"] for r in rows)
    if rows:
        has_reels = fmts.get("reel", 0) > 0
        diversity = len([f for f in fmts if f in ("reel", "carousel", "image")])
        comps.append(_component("format_mix", 40 * has_reels + 20 * diversity, dict(fmts),
                                "40 if Reels are used + 20 per distinct feed format (reel/carousel/image)."))
    else:
        comps.append(_component("format_mix", None, {}, "No synced posts."))

    scored = [c for c in comps if c["score"] is not None]
    total_w = sum(c["weight"] for c in scored)
    overall = round(sum(c["score"] * c["weight"] for c in scored) / total_w) if total_w else None
    return {"score": overall, "components": comps,
            "coverage": f"{len(scored)}/{len(comps)} components had enough data; missing components are excluded and weights re-normalised."}


class AuditNarrative(BaseModel):
    executive_summary: str
    opportunities: list[dict]
    weaknesses: list[dict]
    best_categories: list[str]
    content_gaps: list[str]
    roadmap_30_days: list[dict]


def build_audit(s: Session, account: Account) -> Report:
    health = health_score(s, account)
    perf = performance_summary(s, account, 120)
    rows = media_rows(s, account, utcnow() - dt.timedelta(days=120))
    with_rel = [r for r in rows if r["relative_reach"] is not None]
    outliers = {"outperformers": [r for r in with_rel if r["relative_reach"] >= 1.5][:10],
                "underperformers": [r for r in with_rel if r["relative_reach"] <= 0.6][:10]}
    rules = _rule_findings(account, health, perf)
    data = {"generated_at": utcnow().isoformat(), "health": health, "performance": perf, "outliers": outliers,
            "profile": {k: (account.profile_cache or {}).get(k) for k in
                        ("username", "name", "biography", "website", "followers_count", "media_count", "account_type")},
            "competitors": {"status": "partially_supported",
                            "note": ("Competitor metrics need Meta's business_discovery endpoint, which is only "
                                     "available through the Instagram API with Facebook Login. Competitors listed in "
                                     "Brand Settings are used as qualitative context."),
                            "listed": account.brand.competitors},
            "findings": rules, "labels": {"findings": "Rule-based from measured data",
                                          "narrative": "AI-written interpretation of the data above"}}
    try:
        parsed, source = get_ai().structured(
            s, workflow="profile_audit", schema=AuditNarrative, account_id=account.id,
            system=("You are the head of strategy at a premium Instagram agency writing an account audit. Use only "
                    "the data provided; label anything inferred as a hypothesis; every recommendation needs: why it "
                    "matters, action, priority (high/medium/low), and a measurable success criterion. Each "
                    "opportunity/weakness/roadmap item must be an object with those keys. Five opportunities, five "
                    "weaknesses. Correlation is not causation."),
            content=[{"type": "text", "text": f"Brand: {account.brand.niche} / {account.brand.positioning}; objectives "
                                              f"{account.brand.objectives}.\nAudit data: {data}"}])
        data["narrative"] = parsed.model_dump()
        data["narrative_source"] = source
    except AIUnavailable as exc:
        data["narrative"] = None
        data["narrative_source"] = f"unavailable: {exc}"
    today = utcnow().date()
    report = Report(account_id=account.id, kind="audit", period_start=today - dt.timedelta(days=120),
                    period_end=today, data=data)
    s.add(report)
    return report


def _rule_findings(account: Account, health: dict, perf: dict) -> dict:
    opps, weak, roadmap = [], [], []
    comps = {c["component"]: c for c in health["components"]}
    prof = comps["profile"]["evidence"]
    if prof and not prof.get("has_link"):
        weak.append({"item": "No link in bio", "why": "Profile visitors have no next step",
                     "action": "Add a link-in-bio to the primary landing page with UTM tags", "priority": "high",
                     "success": "Profile link taps reported > 0 within 14 days"})
    if prof and prof.get("niche_keyword_in_name_or_bio") is False:
        opps.append({"item": "Searchable keyword missing from name/bio", "why": "Name and bio text are matched in Instagram search",
                     "action": f"Add a niche keyword (e.g. '{account.brand.niche}') to the display name or bio",
                     "priority": "high", "success": "Profile visits up vs. prior 30 days"})
    cons = comps["consistency"]
    if cons["score"] is not None and cons["score"] < 70:
        weak.append({"item": "Inconsistent posting", "why": "Gaps reset momentum and reduce data for learning",
                     "action": f"Hold {account.posts_per_week} posts/week for 4 weeks", "priority": "high",
                     "success": "8/8 active weeks"})
    eq = comps["engagement_quality"]
    if eq["score"] is not None and eq["score"] < 50:
        opps.append({"item": "Low saves/shares", "why": "Saves and shares signal content worth returning to or sending",
                     "action": "Add one educational carousel per week designed to be saved", "priority": "medium",
                     "success": "Saves+shares share of interactions above 15%"})
    if not perf["by_format"].get("reel"):
        opps.append({"item": "No Reels", "why": "Reels typically drive most non-follower reach",
                     "action": "Publish 2 Reels/week for 4 weeks and compare reach to the feed baseline",
                     "priority": "high", "success": "Reel median reach vs. feed median reach measured over 8 posts"})
    for week in range(1, 5):
        roadmap.append({"week": week, "focus": ["Fix profile + baseline", "Double down on best format",
                                                "Run first controlled test", "Review and reset targets"][week - 1]})
    return {"opportunities": opps, "weaknesses": weak, "roadmap": roadmap}
