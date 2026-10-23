"""Content pipeline job handlers: analyze -> edit -> build posts (copy + QC + approval routing)."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis import analyze_asset, suitability
from .captions import generate_copy
from .editing import asset_dir, concat_reels, make_cover, process_image, process_video
from .jobs import PermanentError, RetryableError, enqueue, handler, on_dead
from .models import Account, Asset, Derivative, Job, Post, PostMedia
from .ops import audit, notify
from .publishing import refresh_review_state

log = logging.getLogger("agency.pipeline")

IN_FLIGHT = ("new", "analyzing", "editing")
MAX_CAROUSEL = 10
MAX_COMPILATION_SECONDS = 90


@handler("analyze_asset")
def analyze_job(s: Session, job: Job) -> dict:
    asset = s.get(Asset, job.payload["asset_id"])
    if asset is None or asset.account_id is None:
        return {"skipped": "asset missing or unassigned"}
    if asset.analysis and asset.status not in ("new", "analyzing"):
        return {"skipped": "already analyzed"}
    asset.status = "analyzing"
    try:
        result = analyze_asset(s, asset)
    except Exception as exc:
        raise RetryableError(f"Analysis failed: {exc}") from exc
    score, reasons = suitability(asset)
    asset.meta = {**(asset.meta or {}), "suitability_score": score, "review_reasons": reasons}
    asset.status = "editing"
    audit(s, "asset.analyzed", account_id=asset.account_id, entity_type="asset", entity_id=asset.id,
          source=asset.analysis_source, score=score)
    enqueue(s, "edit_asset", {"asset_id": asset.id}, account_id=asset.account_id, idempotency_key=f"edit:{asset.id}")
    return {"source": asset.analysis_source, "score": score, "ai": bool(result.get("ai"))}


@handler("edit_asset")
def edit_job(s: Session, job: Job) -> dict:
    asset = s.get(Asset, job.payload["asset_id"])
    account = s.get(Account, asset.account_id)
    brand = account.brand
    for d in list(asset.derivatives):  # idempotent: rebuild exports from the untouched original
        if d.kind != "frame":
            s.delete(d)
    s.flush()
    try:
        if asset.media_kind == "image":
            derivs = process_image(asset, brand.editing_preset if brand else "natural")
        else:
            want_subs = bool((brand.automation_rules or {}).get("subtitles", True)) if brand else True
            derivs = process_video(asset, want_subtitles=want_subs)
    except Exception as exc:
        raise RetryableError(f"Editing failed: {exc}") from exc
    for d in derivs:
        s.add(d)
    asset.status, asset.status_reason = "ready", ""
    s.flush()
    audit(s, "asset.edited", account_id=asset.account_id, entity_type="asset", entity_id=asset.id,
          derivatives=[d.kind for d in derivs], operations=[op["op"] for op in derivs[0].operations])
    group = asset.group_key or f"asset-{asset.id}"
    enqueue(s, "build_posts", {"account_id": asset.account_id, "group_key": asset.group_key, "asset_id": asset.id},
            account_id=asset.account_id, idempotency_key=f"build:{asset.account_id}:{group}:{asset.id}",
            max_attempts=60)
    return {"derivatives": len(derivs)}


@on_dead("analyze_asset")
@on_dead("edit_asset")
def _asset_failed(s: Session, job: Job) -> None:
    asset = s.get(Asset, job.payload["asset_id"])
    if asset is None:
        return
    asset.status, asset.status_reason = "failed", job.last_error or "Processing failed"
    audit(s, "asset.failed", account_id=asset.account_id, entity_type="asset", entity_id=asset.id, error=job.last_error)
    notify(s, f"Could not process {asset.filename}", job.last_error or "", level="error", account_id=asset.account_id,
           link=f"#/library?asset={asset.id}")


def _unposted(s: Session, account_id: int, group_key: str, asset_id: int) -> list[Asset]:
    posted = select(PostMedia.asset_id)
    q = select(Asset).where(Asset.account_id == account_id, Asset.id.not_in(posted))
    q = q.where(Asset.group_key == group_key) if group_key else q.where(Asset.id == asset_id)
    return list(s.scalars(q.order_by(Asset.filename)))


def _derivative(asset: Asset, kind: str) -> Derivative | None:
    return next((d for d in asset.derivatives if d.kind == kind), None)


@handler("build_posts")
def build_posts_job(s: Session, job: Job) -> dict:
    account = s.get(Account, job.payload["account_id"])
    group_key = job.payload.get("group_key") or ""
    assets = _unposted(s, account.id, group_key, job.payload["asset_id"])
    if any(a.status in IN_FLIGHT for a in assets):
        raise RetryableError("Waiting for the rest of the group to finish processing", delay_seconds=60)
    assets = [a for a in assets if a.status == "ready"]
    if not assets:
        return {"skipped": "nothing to build"}
    plans = plan_posts(assets)
    learning = ""
    try:
        from .growth import learning_context

        learning = learning_context(s, account)
    except Exception:  # learning is optional context
        log.exception("learning context unavailable")
    created = []
    for fmt, items in plans:
        post = create_post(s, account, fmt, items)
        generate_copy(s, post, account, learning)
        if post.format == "reel" and post.cover_title:
            _retitle_cover(s, post)
        refresh_review_state(s, post, account)
        created.append(post.id)
        audit(s, "post.drafted", account_id=account.id, entity_type="post", entity_id=post.id, format=fmt,
              status=post.status, reasons=post.approval_reasons)
        if post.status == "approved":
            from .planner import auto_schedule

            auto_schedule(s, post, account)
        else:
            notify(s, f"Post #{post.id} needs your review", "; ".join(post.approval_reasons[:3]),
                   account_id=account.id, link=f"#/studio?post={post.id}")
    return {"posts": created}


def plan_posts(assets: list[Asset]) -> list[tuple[str, list[Asset]]]:
    images = [a for a in assets if a.media_kind == "image"]
    videos = [a for a in assets if a.media_kind == "video"]
    plans: list[tuple[str, list[Asset]]] = []
    if images and videos and len(assets) <= MAX_CAROUSEL:
        return [("carousel", assets)]
    for i in range(0, len(images), MAX_CAROUSEL):
        chunk = images[i:i + MAX_CAROUSEL]
        plans.append(("image" if len(chunk) == 1 else "carousel", chunk))
    total = sum(a.duration or 0 for a in videos)
    if len(videos) > 1 and total <= MAX_COMPILATION_SECONDS:
        plans.append(("reel", videos))
    else:
        plans += [("reel", [v]) for v in videos]
    return plans


def create_post(s: Session, account: Account, fmt: str, assets: list[Asset]) -> Post:
    first_ai = (assets[0].analysis or {}).get("ai") or {}
    post = Post(account_id=account.id, format=fmt, status="draft", pillar=first_ai.get("suggested_pillar", ""))
    s.add(post)
    s.flush()
    if fmt == "reel" and len(assets) > 1:
        reels = [_derivative(a, "reel") for a in assets]
        out = asset_dir(assets[0]) / f"compilation_post{post.id}.mp4"
        duration = concat_reels([d.path for d in reels], out)
        comp = Derivative(asset_id=assets[0].id, kind="reel_compilation", path=str(out), width=1080, height=1920,
                          duration=duration, operations=[{"op": "concat", "assets": [a.id for a in assets]}],
                          qc=reels[0].qc)
        s.add(comp)
        s.flush()
        covers = make_cover(assets[0], out)
        for c in covers:
            c.kind = "cover" if c.kind == "cover" else "preview_compilation"
            s.add(c)
        s.flush()
        cover = next((c for c in covers if c.kind == "cover"), None)
        for i, a in enumerate(assets):
            s.add(PostMedia(post_id=post.id, asset_id=a.id, position=i,
                            derivative_id=comp.id if i == 0 else None,
                            cover_derivative_id=cover.id if (cover and i == 0) else None))
        s.flush()
        s.refresh(post)
        # Only the first item carries the compiled file; the others are recorded as sources.
        post.media[:] = [post.media[0]]
        for a in assets[1:]:
            a.meta = {**(a.meta or {}), "compiled_into_post": post.id}
            a.group_key = f"compiled-into-post-{post.id}"  # keeps the source clip out of later builds
        return post
    for i, a in enumerate(assets):
        if a.media_kind == "video":
            d, cover = _derivative(a, "reel"), _derivative(a, "cover")
        else:
            d, cover = _derivative(a, "portrait" if fmt == "carousel" else "feed"), None
        s.add(PostMedia(post_id=post.id, asset_id=a.id, position=i, derivative_id=d.id if d else None,
                        cover_derivative_id=cover.id if cover else None))
    s.flush()
    s.refresh(post)
    return post


def _retitle_cover(s: Session, post: Post) -> None:
    pm = post.media[0]
    vid = pm.derivative
    if vid is None or not Path(vid.path).exists():
        return
    for c in make_cover(pm.asset, Path(vid.path), post.cover_title):
        if c.kind == "cover":
            s.add(c)
            s.flush()
            pm.cover_derivative_id = c.id
        else:
            s.add(c)


def regenerate_post_copy(s: Session, post: Post, actor: str) -> None:
    account = s.get(Account, post.account_id)
    if post.status in ("published", "publishing"):
        raise PermanentError("Published posts cannot be rewritten")
    from .growth import learning_context

    post.risk_flags = []
    generate_copy(s, post, account, learning_context(s, account))
    post.approved_by = None
    post.status = "draft"
    refresh_review_state(s, post, account)
    audit(s, "post.copy_regenerated", actor=actor, account_id=account.id, entity_type="post", entity_id=post.id)
