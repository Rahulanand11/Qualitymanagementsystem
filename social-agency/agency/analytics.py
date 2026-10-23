"""Analytics ingestion (profile, account insights, media insights, demographics) and aggregation.

Only values returned by the connected source are stored. Anything the source does not return is
recorded in ``MetricAvailability`` with the API's reason, and shown as unavailable in the UI.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connections import NotConnected, client_for, mark_auth_failure
from .instagram import DEMOGRAPHIC_BREAKDOWNS, InstagramError
from .jobs import PermanentError, RetryableError, enqueue, handler
from .models import (Account, AccountMetric, AudienceSnapshot, ConversionMetric, Job, MediaInsightSnapshot,
                     MediaRecord, MetricAvailability, Post, utcnow)
from .ops import audit

METRIC_DEFINITIONS = {
    "followers": "Followers reported on the profile at sync time (followers_count).",
    "net_follower_growth": "Followers at the end of the period minus followers at the start, from our daily profile snapshots. Requires snapshots on both dates.",
    "follows / unfollows": "follows_and_unfollows breakdown from account insights, where the API provides it.",
    "account_reach": "Sum of daily account-level reach. Unique accounts reached over several days is lower than this sum because people seen on multiple days are counted each day.",
    "post_reach_sum": "Sum of reach across posts published in the period. Not comparable to account reach: one person reached by two posts is counted twice.",
    "views": "Number of times content was played or displayed (Instagram's replacement for impressions, which was retired in API v22).",
    "interactions": "likes + comments + shares + saves (+ replies where reported).",
    "engagement_rate_by_reach": "Post interactions / post reach. Account-level: total post interactions / total post reach for posts in the period.",
    "engagement_rate_by_followers": "Post interactions / followers at sync time.",
    "relative_performance": "A post's metric divided by the median of the same metric for the previous 20 posts of the same format on the same account.",
}


def _record_availability(s: Session, account: Account, scope: str, missing: dict[str, str], present: list[str]) -> None:
    existing = {(r.scope, r.metric): r for r in s.scalars(select(MetricAvailability).where(
        MetricAvailability.account_id == account.id))}
    for metric in present:
        row = existing.get((scope, metric))
        if row is None:
            s.add(MetricAvailability(account_id=account.id, scope=scope, metric=metric, available=True))
        else:
            row.available, row.message, row.checked_at = True, "", utcnow()
    for metric, msg in missing.items():
        row = existing.get((scope, metric))
        if row is None:
            s.add(MetricAvailability(account_id=account.id, scope=scope, metric=metric, available=False, message=msg))
        else:
            row.available, row.message, row.checked_at = False, msg, utcnow()
    s.flush()


def _upsert_metric(s: Session, account_id: int, metric: str, day: dt.date, value: float) -> None:
    row = s.scalar(select(AccountMetric).where(AccountMetric.account_id == account_id, AccountMetric.metric == metric,
                                               AccountMetric.day == day))
    if row is None:
        s.add(AccountMetric(account_id=account_id, metric=metric, day=day, value=value))
    else:
        row.value, row.captured_at = value, utcnow()


def _parse_ts(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts.replace("+0000", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)


def sync_account(s: Session, account: Account, *, backfill_days: int = 28, media_limit: int = 200) -> dict:
    stats = {"profile": False, "days": 0, "media": 0, "media_insights": 0, "demographics": 0}
    with client_for(s, account) as client:
        profile = client.profile()
        account.profile_cache = profile
        account.ig_username = profile.get("username") or account.ig_username
        account.profile_synced_at = utcnow()
        today = utcnow().date()
        if profile.get("followers_count") is not None:
            _upsert_metric(s, account.id, "followers_count", today, float(profile["followers_count"]))
        stats["profile"] = True

        # Daily account totals: backfill missing days, always refresh the last 2 days.
        have = {r.day for r in s.scalars(select(AccountMetric).where(AccountMetric.account_id == account.id,
                                                                     AccountMetric.metric == "reach_total"))}
        missing_all: dict[str, str] = {}
        present: set[str] = set()
        for back in range(backfill_days, 0, -1):
            day = today - dt.timedelta(days=back)
            if day in have and back > 2:
                continue
            values, missing = client.account_totals(day, day + dt.timedelta(days=1))
            missing_all.update(missing)
            for k, v in values.items():
                present.add(k.split(":")[0])
                _upsert_metric(s, account.id, f"{k}_total" if ":" not in k else k, day, v)
            stats["days"] += 1
        series, missing = client.account_daily(today - dt.timedelta(days=29), today)
        missing_all.update({k: v for k, v in missing.items()})
        for name, by_day in series.items():
            present.add(name)
            for day, v in by_day.items():
                _upsert_metric(s, account.id, f"{name}_daily", day, v)
        _record_availability(s, account, "account", {k: v for k, v in missing_all.items() if k not in present},
                             sorted(present))

        # Media + per-media insights.
        media_missing: dict[str, str] = {}
        media_present: set[str] = set()
        for m in client.media(limit=media_limit):
            rec = s.scalar(select(MediaRecord).where(MediaRecord.account_id == account.id,
                                                     MediaRecord.ig_media_id == m["id"]))
            if rec is None:
                rec = MediaRecord(account_id=account.id, ig_media_id=m["id"])
                s.add(rec)
                post = s.scalar(select(Post).where(Post.ig_media_id == m["id"], Post.account_id == account.id))
                if post:
                    rec.post_id, rec.pillar = post.id, post.pillar
            rec.media_type = m.get("media_type", "")
            rec.media_product_type = m.get("media_product_type", "")
            rec.caption = m.get("caption", "") or ""
            rec.permalink = m.get("permalink", "") or ""
            rec.thumbnail_url = m.get("thumbnail_url") or m.get("media_url") or ""
            rec.posted_at = _parse_ts(m.get("timestamp"))
            rec.like_count, rec.comments_count = m.get("like_count"), m.get("comments_count")
            stats["media"] += 1
            stale = rec.insights_captured_at is None or utcnow() - rec.insights_captured_at > dt.timedelta(hours=20)
            recent = rec.posted_at is None or utcnow() - rec.posted_at < dt.timedelta(days=90)
            if stale and (recent or rec.insights_captured_at is None):
                values, missing = client.media_insights(m["id"], rec.media_type, rec.media_product_type)
                s.flush()
                s.add(MediaInsightSnapshot(media_record_id=rec.id, metrics=values))
                rec.latest_insights, rec.insights_captured_at = values, utcnow()
                media_present.update(values)
                media_missing.update(missing)
                stats["media_insights"] += 1
        _record_availability(s, account, "media", {k: v for k, v in media_missing.items() if k not in media_present},
                             sorted(media_present))

        # Audience demographics (aggregated by Instagram; never inferred per person).
        for metric in ("follower_demographics", "engaged_audience_demographics"):
            for breakdown in DEMOGRAPHIC_BREAKDOWNS:
                try:
                    data = client.demographics(metric, breakdown)
                    ok = bool(data)
                    msg = "" if ok else "No data returned (Instagram withholds demographics below 100 followers/engagements)"
                except InstagramError as exc:
                    if exc.kind in ("rate_limit", "auth"):
                        raise
                    data, ok, msg = {}, False, str(exc)
                s.add(AudienceSnapshot(account_id=account.id, metric=metric, breakdown=breakdown, data=data,
                                       available=ok, message=msg))
                stats["demographics"] += int(ok)
    account.insights_synced_at = utcnow()
    audit(s, "analytics.synced", account_id=account.id, entity_type="account", entity_id=account.id, **stats)
    return stats


@handler("sync_account")
def sync_account_job(s: Session, job: Job) -> dict:
    account = s.get(Account, job.payload["account_id"])
    try:
        return sync_account(s, account)
    except NotConnected as exc:
        raise PermanentError(str(exc)) from exc
    except InstagramError as exc:
        if exc.kind == "rate_limit":
            raise RetryableError(str(exc), exc.retry_after or 900) from exc
        if exc.kind == "auth":
            s.rollback()
            mark_auth_failure(s, s.get(Account, job.payload["account_id"]), str(exc))
            s.commit()
            raise PermanentError(str(exc)) from exc
        raise RetryableError(str(exc)) from exc


@handler("sync_media_insights")
def sync_media_insights_job(s: Session, job: Job) -> dict:
    post = s.get(Post, job.payload["post_id"])
    if post is None or not post.ig_media_id:
        return {"skipped": True}
    account = s.get(Account, post.account_id)
    rec = s.scalar(select(MediaRecord).where(MediaRecord.account_id == account.id,
                                             MediaRecord.ig_media_id == post.ig_media_id))
    try:
        with client_for(s, account) as client:
            values, missing = client.media_insights(post.ig_media_id, rec.media_type if rec else "",
                                                    rec.media_product_type if rec else "")
    except InstagramError as exc:
        raise RetryableError(str(exc), getattr(exc, "retry_after", None)) from exc
    if rec is not None:
        s.add(MediaInsightSnapshot(media_record_id=rec.id, metrics=values))
        rec.latest_insights, rec.insights_captured_at = values, utcnow()
    # follow-up snapshot at 7 days so performance comparisons use matured numbers
    if job.idempotency_key and job.idempotency_key.endswith(":24h"):
        enqueue(s, "sync_media_insights", {"post_id": post.id}, account_id=account.id,
                idempotency_key=f"insights:{post.id}:7d", run_after=utcnow() + dt.timedelta(days=6))
    return {"metrics": values, "unavailable": list(missing)}


# --------------------------------------------------------------- aggregation
def interactions(m: dict) -> float | None:
    if m.get("total_interactions") is not None:
        return float(m["total_interactions"])
    parts = [m.get(k) for k in ("likes", "comments", "shares", "saved")]
    return float(sum(p for p in parts if p is not None)) if any(p is not None for p in parts) else None


def fmt_of(rec: MediaRecord) -> str:
    if rec.media_product_type == "REELS":
        return "reel"
    if rec.media_type == "CAROUSEL_ALBUM":
        return "carousel"
    if rec.media_product_type == "STORY":
        return "story"
    return "video" if rec.media_type == "VIDEO" else "image"


def media_rows(s: Session, account: Account, start: dt.datetime | None = None, end: dt.datetime | None = None) -> list[dict]:
    q = select(MediaRecord).where(MediaRecord.account_id == account.id)
    if start:
        q = q.where(MediaRecord.posted_at >= start)
    if end:
        q = q.where(MediaRecord.posted_at < end)
    recs = list(s.scalars(q.order_by(MediaRecord.posted_at)))
    all_recs = list(s.scalars(select(MediaRecord).where(MediaRecord.account_id == account.id)
                              .order_by(MediaRecord.posted_at)))
    followers = (account.profile_cache or {}).get("followers_count")
    rows = []
    for rec in recs:
        m = rec.latest_insights or {}
        inter = interactions(m)
        reach = m.get("reach")
        prior = [r for r in all_recs if fmt_of(r) == fmt_of(rec) and r.posted_at and rec.posted_at
                 and r.posted_at < rec.posted_at and (r.latest_insights or {}).get("reach") is not None][-20:]
        base = statistics.median(r.latest_insights["reach"] for r in prior) if len(prior) >= 3 else None
        rows.append({
            "id": rec.id, "ig_media_id": rec.ig_media_id, "post_id": rec.post_id, "format": fmt_of(rec),
            "posted_at": rec.posted_at.isoformat() if rec.posted_at else None, "caption": (rec.caption or "")[:300],
            "permalink": rec.permalink, "thumbnail_url": rec.thumbnail_url, "pillar": rec.pillar,
            "metrics": m, "interactions": inter,
            "er_reach": round(inter / reach, 4) if inter is not None and reach else None,
            "er_followers": round(inter / followers, 4) if inter is not None and followers else None,
            "baseline_reach": base, "relative_reach": round(reach / base, 2) if reach is not None and base else None,
            "insights_captured_at": rec.insights_captured_at.isoformat() if rec.insights_captured_at else None,
        })
    return rows


def _metric_sum(s: Session, account_id: int, metric: str, start: dt.date, end: dt.date) -> float | None:
    vals = [r.value for r in s.scalars(select(AccountMetric).where(AccountMetric.account_id == account_id,
                                                                   AccountMetric.metric == metric,
                                                                   AccountMetric.day >= start, AccountMetric.day < end))]
    return sum(vals) if vals else None


def _snapshot(s: Session, account_id: int, metric: str, day: dt.date, before: bool = True) -> float | None:
    q = select(AccountMetric).where(AccountMetric.account_id == account_id, AccountMetric.metric == metric)
    q = q.where(AccountMetric.day <= day).order_by(AccountMetric.day.desc()) if before else \
        q.where(AccountMetric.day >= day).order_by(AccountMetric.day)
    row = s.scalar(q.limit(1))
    return row.value if row else None


def overview(s: Session, account: Account, start: dt.date, end: dt.date) -> dict:
    """Executive overview for [start, end). Every number carries its definition; missing data is explicit."""
    rows = media_rows(s, account, dt.datetime.combine(start, dt.time()), dt.datetime.combine(end, dt.time()))
    unavailable = {(r.scope, r.metric): r.message for r in s.scalars(select(MetricAvailability).where(
        MetricAvailability.account_id == account.id, MetricAvailability.available.is_(False)))}
    f_start = _snapshot(s, account.id, "followers_count", start, before=True)
    f_end = _snapshot(s, account.id, "followers_count", end - dt.timedelta(days=1), before=True)

    def total(metric: str):
        v = _metric_sum(s, account.id, f"{metric}_total", start, end)
        return {"value": v, "unavailable": unavailable.get(("account", metric)) if v is None else None}

    post_reach = [r["metrics"].get("reach") for r in rows if r["metrics"].get("reach") is not None]
    post_inter = [r["interactions"] for r in rows if r["interactions"] is not None]
    follows = _metric_sum(s, account.id, "follows_and_unfollows:FOLLOWER", start, end)
    unfollows = _metric_sum(s, account.id, "follows_and_unfollows:NON_FOLLOWER", start, end)
    best = sorted([r for r in rows if r["metrics"].get("reach") is not None],
                  key=lambda r: r["metrics"]["reach"], reverse=True)[:5]
    conv = {}
    for c in s.scalars(select(ConversionMetric).where(ConversionMetric.account_id == account.id,
                                                      ConversionMetric.day >= start, ConversionMetric.day < end)):
        conv.setdefault(c.metric, {"value": 0.0, "method": c.method})
        conv[c.metric]["value"] += c.value
    return {
        "period": {"start": str(start), "end": str(end)},
        "data_freshness": {"profile": account.profile_synced_at.isoformat() if account.profile_synced_at else None,
                           "insights": account.insights_synced_at.isoformat() if account.insights_synced_at else None},
        "followers": {"value": (account.profile_cache or {}).get("followers_count")},
        "net_follower_growth": {"value": f_end - f_start if f_start is not None and f_end is not None else None,
                                "unavailable": None if f_start is not None else "No follower snapshot on/before the start date (history starts when the account is connected)"},
        "followers_gained": {"value": follows, "unavailable": None if follows is not None else unavailable.get(("account", "follows_and_unfollows"), "Not reported")},
        "followers_lost": {"value": unfollows, "unavailable": None if unfollows is not None else unavailable.get(("account", "follows_and_unfollows"), "Not reported")},
        "account_reach": total("reach"),
        "views": total("views"),
        "accounts_engaged": total("accounts_engaged"),
        "total_interactions": total("total_interactions"),
        "likes": total("likes"), "comments": total("comments"), "shares": total("shares"), "saves": total("saves"),
        "profile_link_taps": total("profile_links_taps"),
        "impressions": {"value": None, "unavailable": "Retired by Instagram in API v22 (replaced by views)"},
        "post_reach_sum": {"value": sum(post_reach) if post_reach else None},
        "engagement_rate_by_reach": {"value": round(sum(post_inter) / sum(post_reach), 4) if post_inter and post_reach else None},
        "content_published": {"value": len(rows), "by_format": _count(rows, "format")},
        "best_content": best,
        "conversions": conv or {"note": "No conversion data imported. Connect website analytics (CSV import) with a stated attribution method."},
        "definitions": METRIC_DEFINITIONS,
    }


def _count(rows: list[dict], key: str) -> dict:
    out: dict = defaultdict(int)
    for r in rows:
        out[r[key]] += 1
    return dict(out)


def daily_series(s: Session, account: Account, metric: str, start: dt.date, end: dt.date) -> list[dict]:
    return [{"day": str(r.day), "value": r.value, "captured_at": r.captured_at.isoformat()}
            for r in s.scalars(select(AccountMetric).where(AccountMetric.account_id == account.id,
                                                           AccountMetric.metric == metric, AccountMetric.day >= start,
                                                           AccountMetric.day < end).order_by(AccountMetric.day))]


def audience(s: Session, account: Account) -> dict:
    out: dict = {}
    for metric in ("follower_demographics", "engaged_audience_demographics"):
        out[metric] = {}
        for breakdown in DEMOGRAPHIC_BREAKDOWNS:
            snaps = list(s.scalars(select(AudienceSnapshot).where(
                AudienceSnapshot.account_id == account.id, AudienceSnapshot.metric == metric,
                AudienceSnapshot.breakdown == breakdown).order_by(AudienceSnapshot.captured_at.desc()).limit(2)))
            if not snaps:
                out[metric][breakdown] = {"available": False, "message": "Data unavailable from connected source (not synced yet)"}
                continue
            latest = snaps[0]
            entry = {"available": latest.available, "captured_at": latest.captured_at.isoformat(),
                     "data": dict(sorted(latest.data.items(), key=lambda kv: -kv[1])),
                     "message": latest.message or ""}
            if not latest.available:
                entry["message"] = ("Data unavailable from connected source. " + (latest.message or "")
                                    + " Alternative: export Audience data from Instagram's professional dashboard.")
            if len(snaps) > 1 and snaps[1].available and latest.available:
                entry["change"] = {k: v - snaps[1].data.get(k, 0) for k, v in latest.data.items()}
                entry["previous_captured_at"] = snaps[1].captured_at.isoformat()
            out[metric][breakdown] = entry
    target = account.brand.target_audience or {}
    fd = out["follower_demographics"]
    comparison = {}
    if fd.get("country", {}).get("available") and target.get("countries"):
        total = sum(fd["country"]["data"].values()) or 1
        comparison["countries"] = {c: round(100 * fd["country"]["data"].get(c, 0) / total, 1) for c in target["countries"]}
    out["target_comparison"] = comparison or {"note": "Set target countries in Brand Settings and sync demographics to compare."}
    out["policy"] = "Aggregated statistics from Instagram only. Individual followers' gender or location are never inferred."
    return out


def import_conversions_csv(s: Session, account: Account, text: str, method: str) -> int:
    """CSV columns: date,metric,value[,utm_campaign]. Method states how attribution was measured."""
    import csv
    import io

    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        s.add(ConversionMetric(account_id=account.id, day=dt.date.fromisoformat(row["date"]), metric=row["metric"],
                               value=float(row["value"]), utm_campaign=row.get("utm_campaign", ""), method=method))
        n += 1
    audit(s, "conversions.imported", account_id=account.id, rows=n, method=method)
    return n
