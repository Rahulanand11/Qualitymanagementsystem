"""Comment monitoring: classification, lead/complaint detection, drafted replies, guarded sending.

Only replies to comments on the account's own posts (allowed by instagram_business_manage_comments).
Never sends unsolicited messages. Sensitive, negative, urgent or legal items are escalated and never
auto-sent; auto-send applies only to approved templates for low-risk categories.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai
from .connections import client_for
from .instagram import InstagramError
from .jobs import PermanentError, RetryableError, handler
from .models import Account, CommunityItem, Job, MediaRecord, ReplyTemplate, utcnow
from .ops import audit, notify

SPAM = re.compile(r"(https?://|www\.|promote it on|dm (us|me) for (promo|collab)|crypto|forex|bitcoin|cash ?app|"
                  r"send (a )?dm.*(feature|promo)|check my (bio|page)|💰💰)", re.I)
URGENT = re.compile(r"\b(refund|scam|fraud|lawyer|legal|sue|injur\w*|unsafe|allerg\w*|police|broke|broken|"
                    r"never arrived|charged twice|stolen)\b", re.I)
LEAD = re.compile(r"\b(price|cost|how much|buy|order|available|ship|shipping|book|booking|hire|quote|collab|"
                  r"where can i (get|buy)|link)\b", re.I)
NEGATIVE = re.compile(r"\b(bad|worst|terrible|disappoint\w*|awful|hate|rude|poor|waste|angry|never again)\b", re.I)
POSITIVE = re.compile(r"(love|amazing|beautiful|great|awesome|gorgeous|stunning|wow|🔥|😍|❤️|👏|🙌)", re.I)
AUTO_SEND_CATEGORIES = {"praise", "question"}


def classify_rules(text: str) -> dict:
    t = text or ""
    if SPAM.search(t):
        return {"category": "spam", "sentiment": "neutral", "urgent": False}
    urgent = bool(URGENT.search(t))
    if urgent or NEGATIVE.search(t):
        return {"category": "complaint", "sentiment": "negative", "urgent": urgent}
    if LEAD.search(t):
        return {"category": "lead", "sentiment": "neutral", "urgent": False}
    if "?" in t:
        return {"category": "question", "sentiment": "neutral", "urgent": False}
    if POSITIVE.search(t):
        return {"category": "praise", "sentiment": "positive", "urgent": False}
    return {"category": "other", "sentiment": "neutral", "urgent": False}


class CommentTriage(BaseModel):
    comment_id: str
    category: str  # question | lead | complaint | spam | praise | other
    sentiment: str  # positive | neutral | negative
    urgent: bool
    sensitive: bool
    draft_reply: str


class TriageBatch(BaseModel):
    items: list[CommentTriage]


@handler("sync_comments")
def sync_comments_job(s: Session, job: Job) -> dict:
    account = s.get(Account, job.payload["account_id"])
    recent = list(s.scalars(select(MediaRecord).where(MediaRecord.account_id == account.id,
                                                      MediaRecord.posted_at >= utcnow() - dt.timedelta(days=30))))
    new_items: list[CommunityItem] = []
    try:
        with client_for(s, account) as client:
            for rec in recent:
                for c in client.comments(rec.ig_media_id):
                    exists = s.scalar(select(CommunityItem).where(CommunityItem.account_id == account.id,
                                                                  CommunityItem.ig_comment_id == c["id"]))
                    if exists or (c.get("username") or "").lower() == (account.ig_username or "").lower():
                        continue
                    item = CommunityItem(account_id=account.id, ig_comment_id=c["id"], ig_media_id=rec.ig_media_id,
                                         username=c.get("username", ""), text=c.get("text", ""),
                                         commented_at=dt.datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00")).replace(tzinfo=None)
                                         if c.get("timestamp") else None)
                    rule = classify_rules(item.text)
                    item.category, item.sentiment, item.urgent = rule["category"], rule["sentiment"], rule["urgent"]
                    item.classification_source = "rules"
                    s.add(item)
                    new_items.append(item)
    except InstagramError as exc:
        if exc.kind in ("rate_limit", "transient"):
            raise RetryableError(str(exc), exc.retry_after) from exc
        raise PermanentError(str(exc)) from exc
    s.flush()
    triage(s, account, [i for i in new_items if i.category != "spam"])
    for item in new_items:
        item.escalated = item.urgent or item.category == "complaint"
        if item.escalated:
            notify(s, f"Comment needs attention on @{account.ig_username}", f"@{item.username}: {item.text[:140]}",
                   level="warning", account_id=account.id, link="#/community")
        elif item.category == "lead":
            notify(s, "Possible lead in comments", f"@{item.username}: {item.text[:140]}", account_id=account.id,
                   link="#/community")
    sent = auto_reply(s, account, new_items)
    return {"new": len(new_items), "auto_sent": sent}


def triage(s: Session, account: Account, items: list[CommunityItem]) -> None:
    if not items:
        return
    voice = account.brand.voice if account.brand else ""
    templates = {t.category: t.text for t in s.scalars(select(ReplyTemplate).where(ReplyTemplate.account_id == account.id))}
    for i in range(0, len(items), 40):
        chunk = items[i:i + 40]
        listing = "\n".join(f"{c.ig_comment_id} | @{c.username}: {c.text}" for c in chunk)
        try:
            parsed, source = get_ai().structured(
                s, workflow="comment_triage", schema=TriageBatch, account_id=account.id, light=True,
                system=("You triage Instagram comments for a brand. Categories: question, lead, complaint, spam, praise, "
                        "other. Mark urgent for safety, legal, refund or fraud issues. Mark sensitive for anything "
                        "personal, medical, legal, financial or heated. Draft short, warm, specific replies in the brand "
                        "voice; never promise refunds, prices, dates or outcomes; for complaints offer to continue in DMs."),
                content=[{"type": "text", "text": f"Brand voice: {voice}\nApproved templates: {templates}\n\n{listing}"}])
        except AIUnavailable:
            for c in chunk:
                if c.category in templates:
                    c.draft_reply, c.reply_status = templates[c.category], "drafted"
            continue
        by_id = {t.comment_id: t for t in parsed.items}
        for c in chunk:
            t = by_id.get(c.ig_comment_id)
            if t is None:
                continue
            c.category, c.sentiment = t.category, t.sentiment
            c.urgent = c.urgent or t.urgent or (t.sensitive and t.sentiment == "negative")
            c.classification_source = source
            if t.sensitive:
                c.escalated = True
            if t.draft_reply and c.category != "spam":
                c.draft_reply, c.reply_status = t.draft_reply, "drafted"


def auto_reply(s: Session, account: Account, items: list[CommunityItem]) -> int:
    rules = (account.brand.automation_rules or {}) if account.brand else {}
    if account.autonomy_mode == "approval" or not rules.get("auto_reply_templates"):
        return 0
    templates = {t.category: t for t in s.scalars(select(ReplyTemplate).where(
        ReplyTemplate.account_id == account.id, ReplyTemplate.auto_send.is_(True)))}
    sent = 0
    for item in items:
        tpl = templates.get(item.category)
        if tpl is None or item.escalated or item.urgent or item.category not in AUTO_SEND_CATEGORIES:
            continue
        item.draft_reply = tpl.text
        send_reply(s, account, item, actor="auto-template")
        sent += item.reply_status == "sent"
    return sent


def send_reply(s: Session, account: Account, item: CommunityItem, actor: str, text: str | None = None) -> None:
    message = (text or item.draft_reply or "").strip()
    if not message:
        raise ValueError("Reply text is empty")
    try:
        with client_for(s, account) as client:
            client.reply_to_comment(item.ig_comment_id, message)
        item.reply_status, item.replied_at, item.draft_reply, item.reply_error = "sent", utcnow(), message, ""
        audit(s, "community.reply_sent", actor=actor, account_id=account.id, entity_type="comment", entity_id=item.id)
    except InstagramError as exc:
        item.reply_status, item.reply_error = "failed", str(exc)
        audit(s, "community.reply_failed", actor=actor, account_id=account.id, entity_type="comment",
              entity_id=item.id, error=str(exc))


def weekly_summary(s: Session, account: Account, days: int = 7) -> dict:
    since = utcnow() - dt.timedelta(days=days)
    items = list(s.scalars(select(CommunityItem).where(CommunityItem.account_id == account.id,
                                                       CommunityItem.created_at >= since)))
    words = Counter(w for i in items if i.category == "question"
                    for w in re.findall(r"[a-z]{4,}", (i.text or "").lower()))
    return {"total": len(items), "by_category": dict(Counter(i.category for i in items)),
            "by_sentiment": dict(Counter(i.sentiment for i in items)),
            "escalated": sum(i.escalated for i in items), "leads": sum(i.category == "lead" for i in items),
            "replied": sum(i.reply_status == "sent" for i in items),
            "frequent_question_terms": words.most_common(10)}
