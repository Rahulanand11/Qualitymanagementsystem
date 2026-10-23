"""Model -> JSON helpers. Tokens and secrets are never serialised."""
from __future__ import annotations

from ..models import Account, Asset, CommunityItem, Derivative, Experiment, Job, Post


def iso(v):
    return v.isoformat() if v else None


def account(a: Account) -> dict:
    p = a.profile_cache or {}
    return {"id": a.id, "name": a.name, "slug": a.slug, "account_type": a.account_type,
            "ig_username": a.ig_username, "connection_status": a.connection_status,
            "connection_error": a.connection_error, "autonomy_mode": a.autonomy_mode,
            "publishing_paused": a.publishing_paused, "posts_per_week": a.posts_per_week, "timezone": a.timezone,
            "folder_name": a.folder_name, "followers": p.get("followers_count"),
            "profile_picture_url": p.get("profile_picture_url"),
            "profile_synced_at": iso(a.profile_synced_at), "insights_synced_at": iso(a.insights_synced_at),
            "onboarding_complete": bool(a.brand and a.brand.onboarding_complete)}


BRAND_FIELDS = ("category", "niche", "offers", "positioning", "target_audience", "objectives", "voice",
                "visual_identity", "competitors", "inspirations", "website", "landing_pages", "ctas", "languages",
                "markets", "priorities", "content_pillars", "hashtag_bank", "banned_phrases", "editing_preset",
                "automation_rules", "onboarding_complete")


def brand(b) -> dict:
    out = {f: getattr(b, f) for f in BRAND_FIELDS}
    out["ai_suggestions"] = b.ai_suggestions
    out["updated_at"] = iso(b.updated_at)
    return out


def derivative(d: Derivative) -> dict:
    return {"id": d.id, "kind": d.kind, "width": d.width, "height": d.height, "duration": d.duration,
            "operations": d.operations, "qc": d.qc, "url": f"/api/media/{d.id}", "created_at": iso(d.created_at)}


def asset(a: Asset, full: bool = False) -> dict:
    preview = next((d for d in a.derivatives if d.kind == "preview"), None)
    ai = (a.analysis or {}).get("ai") or {}
    out = {"id": a.id, "filename": a.filename, "media_kind": a.media_kind, "account_id": a.account_id,
           "campaign": a.campaign, "status": a.status, "status_reason": a.status_reason,
           "width": a.width, "height": a.height, "duration": a.duration, "size_bytes": a.size_bytes,
           "assignment_source": a.assignment_source, "rights_confirmed": a.rights_confirmed,
           "rights_note": a.rights_note, "analysis_source": a.analysis_source, "summary": ai.get("summary"),
           "suggested_pillar": ai.get("suggested_pillar"), "suitability": (a.meta or {}).get("suitability_score"),
           "review_reasons": (a.meta or {}).get("review_reasons", []),
           "preview_url": f"/api/media/{preview.id}" if preview else None,
           "created_at": iso(a.created_at), "updated_at": iso(a.updated_at)}
    if full:
        out["analysis"] = {k: v for k, v in (a.analysis or {}).items() if k != "frames"}
        out["meta"] = a.meta
        out["derivatives"] = [derivative(d) for d in a.derivatives]
        out["original_url"] = f"/api/assets/{a.id}/original"
    return out


def post(p: Post, full: bool = False) -> dict:
    first = p.media[0] if p.media else None
    preview = None
    if first:
        prev = next((d for d in first.asset.derivatives if d.kind == "preview"), None)
        preview = f"/api/media/{(first.cover or prev or first.derivative).id}" if (first.cover or prev or first.derivative) else None
    out = {"id": p.id, "account_id": p.account_id, "status": p.status, "format": p.format, "pillar": p.pillar,
           "objective": p.objective, "hook": p.hook, "caption": p.caption, "cta": p.cta, "hashtags": p.hashtags,
           "cover_title": p.cover_title, "content_source": p.content_source, "requires_approval": p.requires_approval,
           "approval_reasons": p.approval_reasons, "approved_by": p.approved_by, "risk_flags": p.risk_flags,
           "qc_score": (p.qc or {}).get("score"), "scheduled_for": iso(p.scheduled_for),
           "schedule_source": p.schedule_source, "published_at": iso(p.published_at), "permalink": p.permalink,
           "ig_media_id": p.ig_media_id, "last_error": p.last_error, "publish_attempts": p.publish_attempts,
           "preview_url": preview, "media_count": len(p.media), "created_at": iso(p.created_at),
           "experiment_id": p.experiment_id, "experiment_arm": p.experiment_arm}
    if full:
        out.update({"target_audience": p.target_audience, "concept": p.concept, "keywords": p.keywords,
                    "on_screen_text": p.on_screen_text, "alt_text": p.alt_text, "variants": p.variants, "qc": p.qc,
                    "media": [{"asset_id": pm.asset_id, "position": pm.position, "filename": pm.asset.filename,
                               "media_kind": pm.asset.media_kind,
                               "export": derivative(pm.derivative) if pm.derivative else None,
                               "cover": derivative(pm.cover) if pm.cover else None,
                               "original_url": f"/api/assets/{pm.asset_id}/original",
                               "edit_operations": pm.derivative.operations if pm.derivative else []}
                              for pm in p.media]})
    return out


def job(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "account_id": j.account_id, "status": j.status, "attempts": j.attempts,
            "max_attempts": j.max_attempts, "run_after": iso(j.run_after), "last_error": j.last_error,
            "payload": j.payload, "result": j.result, "created_at": iso(j.created_at), "updated_at": iso(j.updated_at)}


def experiment(e: Experiment) -> dict:
    return {"id": e.id, "account_id": e.account_id, "hypothesis": e.hypothesis, "variable": e.variable,
            "target_metric": e.target_metric, "start_date": iso(e.start_date), "end_date": iso(e.end_date),
            "baseline": e.baseline, "result": e.result, "result_detail": e.result_detail, "confidence": e.confidence,
            "limitations": e.limitations, "decision": e.decision, "status": e.status, "origin": e.origin}


def comment(c: CommunityItem) -> dict:
    return {"id": c.id, "account_id": c.account_id, "username": c.username, "text": c.text,
            "commented_at": iso(c.commented_at), "category": c.category, "sentiment": c.sentiment,
            "urgent": c.urgent, "escalated": c.escalated, "draft_reply": c.draft_reply,
            "reply_status": c.reply_status, "reply_error": c.reply_error, "replied_at": iso(c.replied_at),
            "classification_source": c.classification_source, "ig_media_id": c.ig_media_id}
