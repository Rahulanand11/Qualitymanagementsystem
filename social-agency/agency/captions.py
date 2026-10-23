"""Instagram copy engine: hooks, captions, keywords, hashtags, alt text, cover titles, variants."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .ai import AIUnavailable, get_ai, image_block
from .models import Account, Post


class CopyAngle(BaseModel):
    angle_name: str
    rationale: str
    hook: str = Field(description="First line; must earn the 'more' tap. No clickbait.")
    caption: str = Field(description="Full caption body in the account's voice, without hashtags")
    cta: str
    hashtags: list[str] = Field(description="3-8 specific, relevant hashtags without '#'")
    keywords: list[str] = Field(description="Natural search phrases a person would type into Instagram search")
    on_screen_text: str = Field(description="Short on-screen text for Reels; empty for images")
    alt_text: str = Field(description="Accessible, literal description of the media")
    cover_title: str = Field(description="2-6 word cover title for clarity in the grid and search")


class PostCopy(BaseModel):
    pillar: str
    objective: str
    target_audience: str
    concept: str
    angles: list[CopyAngle] = Field(description="2-3 genuinely different creative angles")
    selected_index: int
    selection_reason: str = Field(description="Why this angle best fits strategy, audience and objective")
    claims_needing_verification: list[str] = Field(description="Any factual, product, price or result claims a human must verify; empty if none")
    spelling_grammar_ok: bool


COPY_SYSTEM = """You are the senior copywriter and strategist at a premium Instagram agency.
Write like a specific human with taste, in the account's established voice, never like generic AI copy.
Rules:
- Ground every statement in what the media shows and the brand facts provided. Never invent products, prices, results, testimonials or events.
- No guarantees of reach or results, no engagement bait ("tag 3 friends", "comment YES"), no keyword stuffing.
- Hashtags: few, specific and relevant to the niche and the post; avoid giant generic tags.
- Keywords are suggestions from language patterns, not verified search-volume data; say nothing about Instagram's ranking algorithm.
- Respect banned phrases. Match the language(s) requested.
- Offer 2-3 genuinely different angles and choose the one with the best strategic fit, not the longest."""


def _brand_brief(account: Account) -> str:
    b = account.brand
    ta = b.target_audience or {}
    pillars = "; ".join(f"{p.get('name')} (purpose: {p.get('purpose', '')}, KPI: {p.get('kpi', '')})"
                        for p in (b.content_pillars or []))
    return (
        f"Account: @{account.ig_username or account.slug} — {account.account_type}\n"
        f"Category/niche: {b.category} / {b.niche}\nPositioning: {b.positioning}\nOffers: {b.offers}\n"
        f"Audience: countries={ta.get('countries', [])}, cities={ta.get('cities', [])}, ages={ta.get('age_groups', [])}; {ta.get('notes', '')}\n"
        f"Objectives: {', '.join(b.objectives or [])}\nVoice: {b.voice}\nPillars: {pillars or 'not defined'}\n"
        f"Preferred CTAs: {b.ctas}\nWebsite: {b.website}\nLanguages: {b.languages or ['English']}\n"
        f"Hashtag bank (approved): {b.hashtag_bank}\nBanned phrases: {b.banned_phrases}"
    )


def clean_hashtags(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        t = re.sub(r"[^\w]", "", t.lstrip("#"))
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:30]


def generate_copy(s: Session, post: Post, account: Account, learning: str = "") -> None:
    """Fill the post's copy fields. Falls back to a clearly-labelled draft that requires review."""
    media_desc, blocks = [], []
    for pm in post.media[:4]:
        ai = (pm.asset.analysis or {}).get("ai") or {}
        media_desc.append(f"- {pm.asset.media_kind}: {ai.get('summary', 'no AI description')} | mood: {ai.get('mood', '')}"
                          f" | products: {ai.get('products_visible', [])} | campaign: {pm.asset.campaign or '-'}")
        img = pm.cover or pm.derivative
        if img is not None and img.path.endswith(".jpg"):
            blocks.append(image_block(img.path, 1024))
    prompt = (
        f"{_brand_brief(account)}\n\nPost format: {post.format}\nSuggested pillar: {post.pillar or 'choose'}\n"
        f"Media:\n" + "\n".join(media_desc)
        + (f"\n\nWhat has worked for this account (measured):\n{learning}" if learning else "")
        + "\n\nWrite the Instagram copy."
    )
    try:
        parsed, source = get_ai().structured(s, workflow="post_copy", schema=PostCopy, system=COPY_SYSTEM,
                                             content=blocks + [{"type": "text", "text": prompt}],
                                             account_id=account.id)
    except AIUnavailable as exc:
        _fallback_copy(post, account, str(exc))
        return
    idx = parsed.selected_index if 0 <= parsed.selected_index < len(parsed.angles) else 0
    chosen = parsed.angles[idx]
    post.pillar = parsed.pillar
    post.objective = parsed.objective
    post.target_audience = parsed.target_audience
    post.concept = parsed.concept
    post.hook = chosen.hook
    body = chosen.caption.strip()
    post.caption = body if body.startswith(chosen.hook.strip()[:20]) else f"{chosen.hook.strip()}\n\n{body}"
    if chosen.cta and chosen.cta.strip() not in post.caption:
        post.caption += f"\n\n{chosen.cta.strip()}"
    post.cta = chosen.cta
    post.hashtags = clean_hashtags(chosen.hashtags)
    post.keywords = chosen.keywords
    post.on_screen_text = chosen.on_screen_text if post.format == "reel" else ""
    post.alt_text = chosen.alt_text
    post.cover_title = chosen.cover_title
    post.variants = [a.model_dump() for a in parsed.angles]
    post.content_source = source
    flags = list(post.risk_flags or [])
    for claim in parsed.claims_needing_verification:
        flags.append(f"claim to verify: {claim}")
    if not parsed.spelling_grammar_ok:
        flags.append("copy flagged its own spelling/grammar")
    post.risk_flags = flags
    post.qc = {**(post.qc or {}), "selection_reason": parsed.selection_reason,
               "keyword_source": "AI-generated suggestions (not verified search data)"}


def _fallback_copy(post: Post, account: Account, reason: str) -> None:
    b = account.brand
    ai_summaries = [((pm.asset.analysis or {}).get("ai") or {}).get("summary") for pm in post.media]
    desc = next((x for x in ai_summaries if x), "")
    topic = post.media[0].asset.campaign if post.media and post.media[0].asset.campaign else ""
    hook = topic or (desc.split(".")[0] if desc else "New post")
    cta = (b.ctas or [""])[0] if b and b.ctas else ""
    post.hook = hook
    post.caption = hook + (f"\n\n{cta}" if cta else "")
    post.cta = cta
    post.hashtags = clean_hashtags((b.hashtag_bank or [])[:5]) if b else []
    post.keywords = []
    post.alt_text = desc
    post.cover_title = hook[:40]
    post.variants = []
    post.content_source = "fallback"
    post.risk_flags = list(post.risk_flags or []) + [f"Draft copy only — AI unavailable ({reason})"]
