"""Growth intelligence: baselines, pattern analysis, learning loop, and the experiment register.

Observed facts (computed from stored metrics) and hypotheses (suggestions to test) are kept in
separate fields everywhere, and every insight carries its supporting numbers.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import media_rows
from .models import Account, Experiment, Post, utcnow
from .ops import audit

MIN_SAMPLE = 3


def _med(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def performance_summary(s: Session, account: Account, days: int = 90) -> dict:
    start = utcnow() - dt.timedelta(days=days)
    rows = [r for r in media_rows(s, account, start) if r["metrics"]]
    by_format: dict[str, list[dict]] = defaultdict(list)
    by_pillar: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_format[r["format"]].append(r)
        by_pillar[r["pillar"] or "unlabelled"].append(r)

    def summarize(group: list[dict]) -> dict:
        return {"posts": len(group),
                "median_reach": _med([g["metrics"].get("reach") for g in group]),
                "median_views": _med([g["metrics"].get("views") for g in group]),
                "median_er_reach": _med([g["er_reach"] for g in group]),
                "median_saves": _med([g["metrics"].get("saved") for g in group]),
                "median_shares": _med([g["metrics"].get("shares") for g in group]),
                "median_follows": _med([g["metrics"].get("follows") for g in group])}

    return {"days": days, "posts": len(rows), "overall": summarize(rows) if rows else {},
            "by_format": {k: summarize(v) for k, v in by_format.items()},
            "by_pillar": {k: summarize(v) for k, v in by_pillar.items()}}


def _hook_style(caption: str) -> str:
    first = (caption or "").strip().split("\n")[0]
    if first.endswith("?"):
        return "question"
    if re.match(r"^\d", first):
        return "number/list"
    if re.search(r"\b(how to|why|what)\b", first.lower()):
        return "how/why"
    if len(first) <= 40:
        return "short statement"
    return "long statement"


def growth_insights(s: Session, account: Account, days: int = 7) -> dict:
    """What drove results, what underperformed, and what to test next."""
    end = utcnow()
    start = end - dt.timedelta(days=days)
    rows = [r for r in media_rows(s, account, start, end) if r["metrics"]]
    hist = [r for r in media_rows(s, account, end - dt.timedelta(days=120), end) if r["metrics"]]
    facts, hypotheses, actions = [], [], []
    if not rows:
        return {"period_days": days, "facts": [], "hypotheses": [],
                "actions": [{"action": "Publish and sync content to build a baseline", "why": "No posts with insights in this period"}],
                "status": "insufficient_data"}

    top_reach = max(rows, key=lambda r: r["metrics"].get("reach") or 0)
    facts.append({"fact": f"Highest reach: {top_reach['format']} post on {top_reach['posted_at'][:10]}",
                  "data": {"reach": top_reach["metrics"].get("reach"), "relative_reach": top_reach["relative_reach"],
                           "permalink": top_reach["permalink"]}})
    er_rows = [r for r in rows if r["er_reach"] is not None]
    if er_rows:
        top_er = max(er_rows, key=lambda r: r["er_reach"])
        facts.append({"fact": f"Strongest engagement rate (by reach): {top_er['er_reach']:.1%}",
                      "data": {"post": top_er["permalink"], "interactions": top_er["interactions"]}})
    follow_rows = [r for r in rows if r["metrics"].get("follows")]
    if follow_rows:
        top_f = max(follow_rows, key=lambda r: r["metrics"]["follows"])
        facts.append({"fact": f"Most follows attributed by Instagram: {int(top_f['metrics']['follows'])} from one post",
                      "data": {"post": top_f["permalink"], "format": top_f["format"]}})

    perf = performance_summary(s, account, 120)
    fmts = {k: v for k, v in perf["by_format"].items() if v["posts"] >= MIN_SAMPLE and v["median_reach"]}
    if len(fmts) >= 2:
        best = max(fmts, key=lambda k: fmts[k]["median_reach"])
        worst = min(fmts, key=lambda k: fmts[k]["median_reach"])
        facts.append({"fact": f"Over 120 days, {best} has the highest median reach and {worst} the lowest",
                      "data": {k: v["median_reach"] for k, v in fmts.items()}})
        hypotheses.append({"hypothesis": f"Shifting one weekly {worst} slot to {best} will raise median weekly reach",
                           "test": {"variable": "format", "target_metric": "reach", "duration_days": 14},
                           "confidence": "low" if min(fmts[best]["posts"], fmts[worst]["posts"]) < 8 else "medium"})
    pillars = {k: v for k, v in perf["by_pillar"].items() if v["posts"] >= MIN_SAMPLE and v["median_reach"]}
    if pillars and perf["overall"].get("median_reach"):
        overall = perf["overall"]["median_reach"]
        for k, v in pillars.items():
            if v["median_reach"] < 0.7 * overall:
                facts.append({"fact": f"Pillar '{k}' is underperforming: median reach {v['median_reach']} vs {overall} overall",
                              "data": v})
                actions.append({"action": f"Rework the '{k}' pillar's hooks or formats before cutting it",
                                "why": "Median reach is below 70% of the account median", "metric": "reach"})

    styles: dict[str, list[float]] = defaultdict(list)
    for r in hist:
        if r["relative_reach"] is not None:
            styles[_hook_style(r["caption"])].append(r["relative_reach"])
    styles = {k: v for k, v in styles.items() if len(v) >= MIN_SAMPLE}
    if len(styles) >= 2:
        best_style = max(styles, key=lambda k: statistics.median(styles[k]))
        hypotheses.append({"hypothesis": f"'{best_style}' openings outperform other hook styles",
                           "evidence": {k: round(statistics.median(v), 2) for k, v in styles.items()},
                           "test": {"variable": "hook_style", "target_metric": "reach", "duration_days": 14},
                           "confidence": "low", "note": "Correlation across different posts; topics and formats also differed."})

    under = [r for r in rows if r["relative_reach"] is not None and r["relative_reach"] < 0.6]
    for r in under[:3]:
        facts.append({"fact": f"Underperformed baseline: {r['format']} on {r['posted_at'][:10]} at {r['relative_reach']}x",
                      "data": {"permalink": r["permalink"]}})
    if not actions:
        actions.append({"action": "Keep the current mix and run one controlled test from the hypotheses",
                        "why": "No pillar fell below the underperformance threshold"})
    return {"period_days": days, "facts": facts, "hypotheses": hypotheses, "actions": actions,
            "labels": {"facts": "Observed from synced Instagram data", "hypotheses": "Generated suggestions; not proven"}}


def target_tracking(s: Session, account: Account) -> dict:
    targets = (account.brand.automation_rules or {}).get("monthly_targets", {})
    if not targets:
        return {"note": "No monthly targets set (Brand Settings → automation rules → monthly_targets)."}
    from .analytics import overview

    today = utcnow().date()
    month_start = today.replace(day=1)
    ov = overview(s, account, month_start, today + dt.timedelta(days=1))
    days_in_month = ((month_start + dt.timedelta(days=32)).replace(day=1) - month_start).days
    elapsed = (today - month_start).days + 1
    out = {}
    for key, target in targets.items():
        actual = (ov.get(key) or {}).get("value")
        projected = actual / elapsed * days_in_month if actual is not None else None
        out[key] = {"target": target, "actual": actual,
                    "projected": round(projected, 1) if projected is not None else None,
                    "on_track": projected >= target if projected is not None else None}
    return out


def learning_context(s: Session, account: Account) -> str:
    """Short, factual summary of what has worked, fed into copy generation."""
    rows = [r for r in media_rows(s, account, utcnow() - dt.timedelta(days=120)) if r["relative_reach"]]
    if len(rows) < MIN_SAMPLE:
        return ""
    best = sorted(rows, key=lambda r: r["relative_reach"], reverse=True)[:3]
    worst = sorted(rows, key=lambda r: r["relative_reach"])[:2]
    lines = ["Top posts by reach vs. baseline:"]
    lines += [f"- {r['relative_reach']}x {r['format']}: \"{r['caption'][:120]}\"" for r in best]
    lines += ["Weakest:"] + [f"- {r['relative_reach']}x {r['format']}: \"{r['caption'][:120]}\"" for r in worst]
    done = s.scalars(select(Experiment).where(Experiment.account_id == account.id, Experiment.status == "evaluated"))
    for e in done:
        lines.append(f"Experiment '{e.variable}': {e.decision} ({e.confidence} confidence) — {e.hypothesis}")
    return "\n".join(lines)


def pillar_adjustments(s: Session, account: Account) -> dict[str, float]:
    """Bounded (+/-30%) weight multipliers per pillar from measured median reach."""
    perf = performance_summary(s, account, 90)
    overall = perf["overall"].get("median_reach") if perf["overall"] else None
    if not overall:
        return {}
    out = {}
    for k, v in perf["by_pillar"].items():
        if v["posts"] >= MIN_SAMPLE and v["median_reach"]:
            out[k] = round(max(0.7, min(1.3, v["median_reach"] / overall)), 2)
    return out


# ------------------------------------------------------------ experiments
def evaluate_experiment(s: Session, exp: Experiment) -> Experiment:
    posts = list(s.scalars(select(Post).where(Post.experiment_id == exp.id, Post.status == "published")))
    account = s.get(Account, exp.account_id)
    rows = {r["post_id"]: r for r in media_rows(s, account) if r["post_id"]}
    metric = exp.target_metric
    arms: dict[str, list[float]] = defaultdict(list)
    for p in posts:
        r = rows.get(p.id)
        if r and r["metrics"].get(metric) is not None:
            arms[p.experiment_arm or "test"].append(r["metrics"][metric])
    test, control = arms.get("test", []), arms.get("control", [])
    baseline = statistics.median(control) if control else exp.baseline
    result = statistics.median(test) if test else None
    exp.baseline, exp.result = baseline, result
    exp.result_detail = {"arms": {k: {"n": len(v), "median": statistics.median(v)} for k, v in arms.items() if v}}
    n = len(test)
    if result is None or baseline in (None, 0):
        exp.confidence, exp.decision = "none", "test_again"
        exp.limitations = "Not enough published posts with this metric yet."
    else:
        lift = result / baseline - 1
        exp.result_detail["lift"] = round(lift, 3)
        exp.confidence = "low" if n < 5 else ("medium" if n < 12 else "high")
        exp.decision = "continue" if lift > 0.1 else ("stop" if lift < -0.1 else "test_again")
        exp.limitations = (f"{n} test posts; medians compared without randomisation. Topic, timing and "
                           f"platform changes may also explain the difference.")
    exp.status = "evaluated"
    audit(s, "experiment.evaluated", account_id=exp.account_id, entity_type="experiment", entity_id=exp.id,
          decision=exp.decision, confidence=exp.confidence)
    return exp
