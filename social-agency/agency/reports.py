"""Weekly and monthly reports from stored data, exportable to CSV and PDF."""
from __future__ import annotations

import csv
import datetime as dt
import io

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai
from .analytics import audience, media_rows, overview
from .community import weekly_summary
from .growth import growth_insights, target_tracking
from .jobs import handler
from .models import Account, Experiment, Job, Post, Report, utcnow
from .ops import audit, notify


class ReportNarrative(BaseModel):
    summary: str
    problems: list[str]
    next_actions: list[str]


def _val(d: dict, key: str):
    v = d.get(key) or {}
    return v.get("value") if isinstance(v, dict) else v


def build_report(s: Session, account: Account, kind: str, end: dt.date | None = None) -> Report:
    end = end or utcnow().date()
    if kind == "weekly":
        start = end - dt.timedelta(days=7)
        prev_start = start - dt.timedelta(days=7)
    else:  # the last full calendar month
        end = end.replace(day=1)
        start = (end - dt.timedelta(days=1)).replace(day=1)
        prev_start = (start - dt.timedelta(days=1)).replace(day=1)
    cur = overview(s, account, start, end)
    prev = overview(s, account, prev_start, start)
    comparisons = {}
    for key in ("account_reach", "views", "total_interactions", "post_reach_sum", "net_follower_growth",
                "profile_link_taps", "shares", "saves"):
        a, b = _val(cur, key), _val(prev, key)
        comparisons[key] = {"current": a, "previous": b,
                            "change_pct": round(100 * (a - b) / b, 1) if a is not None and b not in (None, 0) else None}
    rows = media_rows(s, account, dt.datetime.combine(start, dt.time()), dt.datetime.combine(end, dt.time()))
    ranked = sorted([r for r in rows if r["metrics"].get("reach") is not None], key=lambda r: r["metrics"]["reach"])
    planned = s.scalars(select(Post).where(Post.account_id == account.id,
                                           Post.scheduled_for >= dt.datetime.combine(start, dt.time()),
                                           Post.scheduled_for < dt.datetime.combine(end, dt.time()))).all()
    published = [p for p in planned if p.status == "published"]
    failed = [p for p in planned if p.status == "failed"]
    exps = s.scalars(select(Experiment).where(Experiment.account_id == account.id,
                                              Experiment.updated_at >= dt.datetime.combine(start, dt.time()))).all()
    data = {
        "account": {"id": account.id, "name": account.name, "username": account.ig_username},
        "kind": kind, "period": {"start": str(start), "end": str(end)},
        "previous_period": {"start": str(prev_start), "end": str(start)},
        "generated_at": utcnow().isoformat(),
        "overview": cur, "comparisons": comparisons,
        "best_content": list(reversed(ranked[-3:])), "weakest_content": ranked[:3],
        "publishing": {"planned": len(planned), "published": len(published), "failed": len(failed),
                       "target_per_week": account.posts_per_week},
        "audience": audience(s, account),
        "growth": growth_insights(s, account, 7 if kind == "weekly" else 30),
        "experiments": [{"hypothesis": e.hypothesis, "variable": e.variable, "decision": e.decision,
                         "confidence": e.confidence, "result_detail": e.result_detail} for e in exps],
        "community": weekly_summary(s, account, 7 if kind == "weekly" else 30),
        "targets": target_tracking(s, account),
    }
    try:
        parsed, source = get_ai().structured(
            s, workflow=f"{kind}_report", schema=ReportNarrative, account_id=account.id,
            system=("Write a concise agency performance summary from the data. Only state numbers present in the data; "
                    "say 'not available' for missing metrics; give specific next actions; no guarantees."),
            content=[{"type": "text", "text": str({k: v for k, v in data.items() if k != "audience"})[:60000]}])
        data["narrative"], data["narrative_source"] = parsed.model_dump(), source
    except AIUnavailable as exc:
        data["narrative"], data["narrative_source"] = None, f"unavailable: {exc}"
    if kind == "monthly":
        from .planner import monthly_plan

        data["next_month_plan"] = monthly_plan(s, account, end).plan
    report = Report(account_id=account.id, kind=kind, period_start=start, period_end=end, data=data)
    s.add(report)
    s.flush()
    audit(s, f"report.{kind}_generated", account_id=account.id, entity_type="report", entity_id=report.id)
    return report


@handler("build_report")
def build_report_job(s: Session, job: Job) -> dict:
    account = s.get(Account, job.payload["account_id"])
    r = build_report(s, account, job.payload["kind"])
    notify(s, f"{job.payload['kind'].title()} report ready for {account.name}", account_id=account.id,
           link=f"#/reports?id={r.id}")
    return {"report_id": r.id}


# ------------------------------------------------------------------ exports
def report_csv(report: Report) -> str:
    d = report.data
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "metric", "current", "previous", "change_pct", "note"])
    for k, v in d.get("comparisons", {}).items():
        w.writerow(["summary", k, v["current"], v["previous"], v["change_pct"], ""])
    ov = d.get("overview", {})
    for k in ("followers", "followers_gained", "followers_lost", "accounts_engaged", "likes", "comments",
              "engagement_rate_by_reach", "impressions"):
        v = ov.get(k) or {}
        w.writerow(["overview", k, v.get("value"), "", "", v.get("unavailable") or ""])
    w.writerow([])
    w.writerow(["content", "posted_at", "format", "reach", "views", "interactions", "er_reach", "relative_reach", "permalink"])
    for r in d.get("best_content", []) + d.get("weakest_content", []):
        m = r.get("metrics", {})
        w.writerow(["content", r.get("posted_at"), r.get("format"), m.get("reach"), m.get("views"), r.get("interactions"),
                    r.get("er_reach"), r.get("relative_reach"), r.get("permalink")])
    w.writerow([])
    for bd, entry in (d.get("audience", {}).get("follower_demographics") or {}).items():
        if entry.get("available"):
            for k, v in entry["data"].items():
                w.writerow(["audience", bd, k, v, "", entry.get("captured_at")])
        else:
            w.writerow(["audience", bd, "", "", "", entry.get("message")])
    return buf.getvalue()


def report_pdf(report: Report) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    d = report.data
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"{d['kind'].title()} report", leftMargin=40, rightMargin=40)
    st = getSampleStyleSheet()
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    story = [Paragraph(f"{esc(d['account']['name'])} — {d['kind'].title()} report", st["Title"]),
             Paragraph(f"{d['period']['start']} to {d['period']['end']} · generated {d['generated_at'][:16]} UTC", st["Normal"]),
             Spacer(1, 12)]
    if d.get("narrative"):
        story += [Paragraph("Summary", st["Heading2"]), Paragraph(esc(d["narrative"]["summary"]), st["BodyText"])]
    story.append(Paragraph("Results vs previous period", st["Heading2"]))
    rows = [["Metric", "Current", "Previous", "Change"]]
    for k, v in d["comparisons"].items():
        rows.append([k.replace("_", " "), _fmt(v["current"]), _fmt(v["previous"]),
                     f"{v['change_pct']}%" if v["change_pct"] is not None else "n/a"])
    t = Table(rows, hAlign="LEFT", colWidths=[180, 90, 90, 70])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                           ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story += [t, Paragraph("n/a = data unavailable from the connected source for that period.", st["Italic"])]
    story.append(Paragraph("Best content", st["Heading2"]))
    for r in d.get("best_content", []):
        story.append(Paragraph(f"{r['posted_at'][:10]} · {r['format']} · reach {_fmt(r['metrics'].get('reach'))} · "
                               f"{esc(r['caption'][:120])}", st["BodyText"]))
    g = d.get("growth", {})
    story.append(Paragraph("Observed facts", st["Heading2"]))
    story += [Paragraph("• " + esc(f["fact"]), st["BodyText"]) for f in g.get("facts", [])] or [Paragraph("None yet.", st["BodyText"])]
    story.append(Paragraph("Hypotheses to test (not proven)", st["Heading2"]))
    story += [Paragraph("• " + esc(h["hypothesis"]), st["BodyText"]) for h in g.get("hypotheses", [])] or [Paragraph("None yet.", st["BodyText"])]
    story.append(Paragraph("Recommended actions", st["Heading2"]))
    actions = (d.get("narrative") or {}).get("next_actions") or [a["action"] for a in g.get("actions", [])]
    story += [Paragraph("• " + esc(a), st["BodyText"]) for a in actions]
    p = d["publishing"]
    story += [Paragraph("Publishing", st["Heading2"]),
              Paragraph(f"Planned {p['planned']}, published {p['published']}, failed {p['failed']}.", st["BodyText"])]
    doc.build(story)
    return buf.getvalue()


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() or abs(v) >= 100 else f"{v:,.3f}"
    return str(v)
