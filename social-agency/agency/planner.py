"""Weekly and monthly content planning, posting-time selection and auto-scheduling."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai
from .models import Account, Asset, ContentPlan, MediaRecord, Post, utcnow
from .ops import audit

# Used until the account has enough of its own history. Labelled as an assumption in the UI.
DEFAULT_SLOTS = [(1, 11), (2, 18), (3, 12), (4, 18), (6, 10), (0, 18), (5, 11)]  # (weekday, hour) local
MIN_POSTS_FOR_TIMING = 12


def _tz(account: Account) -> ZoneInfo:
    try:
        return ZoneInfo(account.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def best_times(s: Session, account: Account) -> dict:
    tz = _tz(account)
    recs = [r for r in s.scalars(select(MediaRecord).where(MediaRecord.account_id == account.id))
            if r.posted_at and (r.latest_insights or {}).get("reach")]
    if len(recs) < MIN_POSTS_FOR_TIMING:
        return {"slots": DEFAULT_SLOTS, "source": "default",
                "note": f"Default schedule (assumption). Needs {MIN_POSTS_FOR_TIMING}+ posts with reach data; has {len(recs)}."}
    median = statistics.median(r.latest_insights["reach"] for r in recs) or 1
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in recs:
        local = r.posted_at.replace(tzinfo=dt.timezone.utc).astimezone(tz)
        buckets[(local.weekday(), local.hour)].append(r.latest_insights["reach"] / median)
    ranked = sorted(((statistics.mean(v), k, len(v)) for k, v in buckets.items() if len(v) >= 2), reverse=True)
    slots = [k for _, k, _ in ranked[:7]]
    for d in DEFAULT_SLOTS:  # top up with defaults on days not covered
        if len(slots) >= 7:
            break
        if d[0] not in {x[0] for x in slots}:
            slots.append(d)
    return {"slots": slots, "source": "account_history",
            "evidence": [{"weekday": k[0], "hour": k[1], "relative_reach": round(m, 2), "posts": n} for m, k, n in ranked[:7]],
            "note": "Ranked by reach relative to the account median. Correlation, not proof of causation."}


def next_slot(s: Session, account: Account, after: dt.datetime | None = None) -> dt.datetime:
    tz = _tz(account)
    after = after or utcnow()
    slots = best_times(s, account)["slots"]
    taken = [p.scheduled_for for p in s.scalars(select(Post).where(
        Post.account_id == account.id, Post.status.in_(("scheduled", "publishing")), Post.scheduled_for.is_not(None)))]
    per_week = Counter((t.isocalendar()[0], t.isocalendar()[1]) for t in taken)
    start_local = after.replace(tzinfo=dt.timezone.utc).astimezone(tz)
    for day in range(0, 60):
        date = (start_local + dt.timedelta(days=day)).date()
        for weekday, hour in sorted(slots, key=lambda x: x[1]):
            if date.weekday() != weekday:
                continue
            cand = dt.datetime(date.year, date.month, date.day, hour, tzinfo=tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
            if cand <= after + dt.timedelta(minutes=10):
                continue
            wk = (cand.isocalendar()[0], cand.isocalendar()[1])
            if per_week[wk] >= account.posts_per_week:
                continue
            if any(abs((cand - t).total_seconds()) < 4 * 3600 for t in taken):
                continue
            return cand
    return after + dt.timedelta(days=1)


def auto_schedule(s: Session, post: Post, account: Account) -> None:
    """In limited/full autonomy, approved posts go straight into the next free slot."""
    if account.autonomy_mode == "approval" or post.status != "approved":
        return
    from .publishing import schedule_post

    schedule_post(s, post, next_slot(s, account), source="auto")


# ------------------------------------------------------------------ weekly
def pillar_weights(s: Session, account: Account) -> dict[str, float]:
    pillars = account.brand.content_pillars or []
    base = {p["name"]: float(p.get("weight", 1)) for p in pillars if p.get("name")}
    if not base:
        return {}
    try:
        from .growth import pillar_adjustments

        adj = pillar_adjustments(s, account)
    except Exception:
        adj = {}
    weights = {k: v * adj.get(k, 1.0) for k, v in base.items()}
    total = sum(weights.values()) or 1
    return {k: round(v / total, 3) for k, v in weights.items()}


def weekly_plan(s: Session, account: Account, week_start: dt.date | None = None) -> ContentPlan:
    tz = _tz(account)
    today = utcnow().replace(tzinfo=dt.timezone.utc).astimezone(tz).date()
    week_start = week_start or (today - dt.timedelta(days=today.weekday()))
    week_end = week_start + dt.timedelta(days=7)
    timing = best_times(s, account)
    slots = sorted(timing["slots"], key=lambda x: (x[0], x[1]))[: account.posts_per_week]
    weights = pillar_weights(s, account)

    def to_utc(d: dt.date, hour: int) -> dt.datetime:
        return dt.datetime(d.year, d.month, d.day, hour, tzinfo=tz).astimezone(dt.timezone.utc).replace(tzinfo=None)

    start_utc, end_utc = to_utc(week_start, 0), to_utc(week_end, 0)
    scheduled = list(s.scalars(select(Post).where(Post.account_id == account.id,
                                                  Post.scheduled_for >= start_utc, Post.scheduled_for < end_utc)))
    backlog = list(s.scalars(select(Post).where(Post.account_id == account.id,
                                                Post.status.in_(("approved", "needs_review", "draft")),
                                                Post.scheduled_for.is_(None)).order_by(Post.created_at)))
    target_counts = {k: round(v * account.posts_per_week) for k, v in weights.items()}
    used = Counter(p.pillar for p in scheduled)
    entries = []
    for p in scheduled:
        entries.append(_entry(p, p.scheduled_for, "scheduled"))
    free_slots = [to_utc(week_start + dt.timedelta(days=wd), h) for wd, h in slots
                  if not any(abs((to_utc(week_start + dt.timedelta(days=wd), h) - x.scheduled_for).total_seconds()) < 4 * 3600
                             for x in scheduled)]
    for when in free_slots[: max(account.posts_per_week - len(scheduled), 0)]:
        if when < utcnow():
            continue
        backlog.sort(key=lambda p: (used[p.pillar] - target_counts.get(p.pillar, 0), p.status != "approved"))
        if backlog:
            p = backlog.pop(0)
            used[p.pillar] += 1
            entries.append(_entry(p, when, "proposed"))
        else:
            need = min(weights, key=lambda k: used[k] - target_counts.get(k, 0)) if weights else "any"
            used[need] += 1
            entries.append({"when": when.isoformat(), "status": "content_gap", "pillar": need,
                            "message": f"No ready content. Upload media for the '{need}' pillar."})
    entries.sort(key=lambda e: e["when"])
    inventory = Counter(a.status for a in s.scalars(select(Asset).where(Asset.account_id == account.id)))
    plan = ContentPlan(account_id=account.id, period_type="week", start_date=week_start, source="planner",
                       plan={"entries": entries, "timing": timing, "pillar_weights": weights,
                             "inventory": dict(inventory), "timezone": str(tz)})
    s.add(plan)
    audit(s, "plan.weekly_created", account_id=account.id, entity_type="plan", week=str(week_start))
    return plan


def _entry(p: Post, when: dt.datetime, state: str) -> dict:
    return {"when": when.isoformat(), "status": state, "post_id": p.id, "post_status": p.status,
            "format": p.format, "pillar": p.pillar, "objective": p.objective, "audience": p.target_audience,
            "concept": p.concept, "hook": p.hook, "caption": p.caption, "cta": p.cta, "hashtags": p.hashtags,
            "keywords": p.keywords, "cover_title": p.cover_title,
            "assets": [pm.asset_id for pm in p.media],
            "expected": f"Beat the account's {p.format} median reach" if p.format else ""}


def apply_weekly_plan(s: Session, plan: ContentPlan, actor: str) -> list[int]:
    """Schedule every approved post in a plan at its proposed time."""
    from .publishing import schedule_post

    done = []
    for e in plan.plan.get("entries", []):
        if e.get("status") != "proposed":
            continue
        post = s.get(Post, e["post_id"])
        if post and post.status == "approved":
            schedule_post(s, post, dt.datetime.fromisoformat(e["when"]), source="weekly_plan", actor=actor)
            e["status"] = "scheduled"
            done.append(post.id)
    plan.plan = dict(plan.plan)
    return done


# ----------------------------------------------------------------- monthly
class MonthlyStrategy(BaseModel):
    campaign_themes: list[str]
    weekly_focus: list[str] = Field(description="One line per week of the month")
    key_dates: list[str] = Field(description="Relevant dates/events for the account's markets; only well-known ones")
    pillar_allocation: dict[str, int] = Field(description="Pillar name -> percent of posts")
    format_mix: dict[str, int] = Field(description="reel/carousel/image/story -> number of posts")
    brand_vs_conversion: str
    repurposing: list[str]
    production_needs: list[str]
    growth_targets: list[str] = Field(description="Measurable targets relative to the measured baseline; no guarantees")
    testing_priorities: list[str]
    rationale: str


def monthly_plan(s: Session, account: Account, month_start: dt.date | None = None) -> ContentPlan:
    from .growth import performance_summary

    today = utcnow().date()
    month_start = month_start or today.replace(day=1)
    perf = performance_summary(s, account, days=90)
    b = account.brand
    data = {"performance_90d": perf, "posts_per_week": account.posts_per_week,
            "pillars": b.content_pillars, "objectives": b.objectives, "markets": b.markets}
    try:
        parsed, source = get_ai().structured(
            s, workflow="monthly_strategy", schema=MonthlyStrategy, account_id=account.id,
            system=("You are the strategy director of a premium Instagram agency. Build next month's plan from the "
                    "measured data. Separate facts from hypotheses, never promise follower counts or virality, and only "
                    "chase trends that fit the brand."),
            content=[{"type": "text", "text": f"Month starting {month_start}. Brand: {b.niche}; {b.positioning}.\n"
                                              f"Data (JSON): {data}"}])
        strategy = parsed.model_dump()
    except AIUnavailable as exc:
        source = "data_rules"
        strategy = _rule_based_month(account, perf, str(exc))
    plan = ContentPlan(account_id=account.id, period_type="month", start_date=month_start, source=source,
                       plan={"strategy": strategy, "data_used": perf})
    s.add(plan)
    audit(s, "plan.monthly_created", account_id=account.id, entity_type="plan", month=str(month_start), source=source)
    return plan


def _rule_based_month(account: Account, perf: dict, reason: str) -> dict:
    n = account.posts_per_week * 4
    by_format = perf.get("by_format", {})
    best = max(by_format, key=lambda f: by_format[f].get("median_reach") or 0) if by_format else "reel"
    mix = {"reel": round(n * 0.5), "carousel": round(n * 0.3), "image": n - round(n * 0.5) - round(n * 0.3)}
    if best in mix:
        mix[best] += 1
    pillars = [p["name"] for p in account.brand.content_pillars or []] or ["(define pillars in Brand Settings)"]
    return {
        "campaign_themes": [f"Strengthen '{p}'" for p in pillars[:3]],
        "weekly_focus": ["Week 1: baseline", "Week 2: best format", "Week 3: test", "Week 4: review"],
        "key_dates": [], "pillar_allocation": {p: round(100 / len(pillars)) for p in pillars},
        "format_mix": mix, "brand_vs_conversion": "80% brand-building / 20% conversion until conversion tracking exists",
        "repurposing": ["Turn top carousel into a Reel", "Cut Reels into Stories"],
        "production_needs": [f"{mix['reel']} vertical clips", f"{mix['carousel']} photo sets"],
        "growth_targets": ["Median reach above last 90-day median"] if perf.get("posts") else ["Establish a baseline"],
        "testing_priorities": ["Hook style on Reels", "Posting time"],
        "rationale": f"Rule-based plan (AI unavailable: {reason}). Best measured format: {best}.",
    }
