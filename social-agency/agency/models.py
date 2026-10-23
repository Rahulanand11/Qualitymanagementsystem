"""ORM models. Every account-owned row carries ``account_id`` so data stays isolated per account."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- users
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner | editor | viewer


# ------------------------------------------------------------------------ accounts
AUTONOMY_MODES = ("approval", "limited", "full")


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    account_type: Mapped[str] = mapped_column(String(20), default="creator")  # creator | business
    ig_username: Mapped[Optional[str]] = mapped_column(String(120))
    ig_user_id: Mapped[Optional[str]] = mapped_column(String(64))
    connection_status: Mapped[str] = mapped_column(String(30), default="not_connected")
    connection_error: Mapped[Optional[str]] = mapped_column(Text)
    autonomy_mode: Mapped[str] = mapped_column(String(20), default="approval")
    publishing_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    posts_per_week: Mapped[int] = mapped_column(Integer, default=4)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    folder_name: Mapped[str] = mapped_column(String(120))  # e.g. "Creator Account"
    profile_cache: Mapped[dict] = mapped_column(JSON, default=dict)
    profile_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    insights_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)

    brand: Mapped["BrandProfile"] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")


class BrandProfile(Base, TimestampMixin):
    """Approved brand rules. AI suggestions are stored separately and never overwrite these silently."""

    __tablename__ = "brand_profiles"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    niche: Mapped[str] = mapped_column(String(255), default="")
    offers: Mapped[str] = mapped_column(Text, default="")
    positioning: Mapped[str] = mapped_column(Text, default="")
    target_audience: Mapped[dict] = mapped_column(JSON, default=dict)  # countries, cities, age_groups, notes
    objectives: Mapped[list] = mapped_column(JSON, default=list)
    voice: Mapped[str] = mapped_column(Text, default="")
    visual_identity: Mapped[dict] = mapped_column(JSON, default=dict)  # colors, fonts, references
    competitors: Mapped[list] = mapped_column(JSON, default=list)
    inspirations: Mapped[list] = mapped_column(JSON, default=list)
    website: Mapped[str] = mapped_column(String(500), default="")
    landing_pages: Mapped[list] = mapped_column(JSON, default=list)
    ctas: Mapped[list] = mapped_column(JSON, default=list)
    languages: Mapped[list] = mapped_column(JSON, default=list)
    markets: Mapped[list] = mapped_column(JSON, default=list)
    priorities: Mapped[str] = mapped_column(Text, default="")
    content_pillars: Mapped[list] = mapped_column(JSON, default=list)  # [{name, purpose, audience, kpi, weight}]
    hashtag_bank: Mapped[list] = mapped_column(JSON, default=list)
    banned_phrases: Mapped[list] = mapped_column(JSON, default=list)
    editing_preset: Mapped[str] = mapped_column(String(40), default="natural")
    # Automation rules, e.g. {"auto_formats": ["image","carousel"], "min_qc_score": 85,
    # "uploads_are_owned": true, "require_subtitles": false, "auto_reply_templates": false}
    automation_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_suggestions: Mapped[dict] = mapped_column(JSON, default=dict)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    account: Mapped[Account] = relationship(back_populates="brand")


class Credential(Base, TimestampMixin):
    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="instagram")
    encrypted_token: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# -------------------------------------------------------------------------- assets
ASSET_STATUSES = (
    "new", "analyzing", "editing", "ready", "scheduled", "published", "failed", "needs_review", "archived",
)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(500))
    media_kind: Mapped[str] = mapped_column(String(10))  # image | video
    mime: Mapped[str] = mapped_column(String(80), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    duration: Mapped[Optional[float]] = mapped_column(Float)
    aspect_ratio: Mapped[Optional[float]] = mapped_column(Float)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), index=True)
    assignment_source: Mapped[str] = mapped_column(String(40), default="")  # folder | filename | sidecar | dashboard
    campaign: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_source: Mapped[str] = mapped_column(String(40), default="")  # ai:<model> | heuristic
    group_key: Mapped[str] = mapped_column(String(120), default="", index=True)
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_note: Mapped[str] = mapped_column(Text, default="")

    derivatives: Mapped[list["Derivative"]] = relationship(back_populates="asset", cascade="all, delete-orphan")


class Derivative(Base):
    __tablename__ = "derivatives"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # feed | square | story | reel | cover | preview
    path: Mapped[str] = mapped_column(Text)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    duration: Mapped[Optional[float]] = mapped_column(Float)
    operations: Mapped[list] = mapped_column(JSON, default=list)
    qc: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="derivatives")


# --------------------------------------------------------------------------- posts
POST_STATUSES = (
    "draft", "needs_review", "approved", "scheduled", "publishing", "published", "failed", "cancelled",
)


class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    format: Mapped[str] = mapped_column(String(20))  # image | carousel | reel
    pillar: Mapped[str] = mapped_column(String(120), default="")
    objective: Mapped[str] = mapped_column(String(120), default="")
    target_audience: Mapped[str] = mapped_column(Text, default="")
    concept: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    on_screen_text: Mapped[str] = mapped_column(Text, default="")
    alt_text: Mapped[str] = mapped_column(Text, default="")
    cover_title: Mapped[str] = mapped_column(String(200), default="")
    variants: Mapped[list] = mapped_column(JSON, default=list)
    content_source: Mapped[str] = mapped_column(String(40), default="")  # ai:<model> | fallback | manual
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    qc: Mapped[dict] = mapped_column(JSON, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_reasons: Mapped[list] = mapped_column(JSON, default=list)
    approved_by: Mapped[Optional[str]] = mapped_column(String(255))
    scheduled_for: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, index=True)  # UTC
    schedule_source: Mapped[str] = mapped_column(String(40), default="")
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    ig_container_id: Mapped[Optional[str]] = mapped_column(String(64))
    ig_media_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    permalink: Mapped[Optional[str]] = mapped_column(Text)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    experiment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("experiments.id"))
    experiment_arm: Mapped[str] = mapped_column(String(40), default="")

    media: Mapped[list["PostMedia"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="PostMedia.position"
    )


class PostMedia(Base):
    __tablename__ = "post_media"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    derivative_id: Mapped[Optional[int]] = mapped_column(ForeignKey("derivatives.id"))
    cover_derivative_id: Mapped[Optional[int]] = mapped_column(ForeignKey("derivatives.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)

    post: Mapped[Post] = relationship(back_populates="media")
    asset: Mapped[Asset] = relationship(foreign_keys=[asset_id])
    derivative: Mapped[Optional[Derivative]] = relationship(foreign_keys=[derivative_id])
    cover: Mapped[Optional[Derivative]] = relationship(foreign_keys=[cover_derivative_id])


class ContentPlan(Base):
    __tablename__ = "content_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    period_type: Mapped[str] = mapped_column(String(10))  # week | month
    start_date: Mapped[dt.date] = mapped_column(Date)
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------------------------- jobs
class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|succeeded|failed|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    locked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)


# -------------------------------------------------------------------- analytics
class AccountMetric(Base):
    """One value of one account-level metric for one day, as returned by the API."""

    __tablename__ = "account_metrics"
    __table_args__ = (UniqueConstraint("account_id", "metric", "day", name="uq_account_metric_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    metric: Mapped[str] = mapped_column(String(60))
    day: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="instagram_api")
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MetricAvailability(Base):
    """Records which metrics the connected source did or did not return, and why."""

    __tablename__ = "metric_availability"
    __table_args__ = (UniqueConstraint("account_id", "scope", "metric", name="uq_metric_avail"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))  # account | media | audience
    metric: Mapped[str] = mapped_column(String(80))
    available: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MediaRecord(Base, TimestampMixin):
    """A post that exists on Instagram (published by us or historically)."""

    __tablename__ = "media_records"
    __table_args__ = (UniqueConstraint("account_id", "ig_media_id", name="uq_media_account"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ig_media_id: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(30), default="")  # IMAGE | VIDEO | CAROUSEL_ALBUM
    media_product_type: Mapped[str] = mapped_column(String(30), default="")  # FEED | REELS | STORY
    caption: Mapped[str] = mapped_column(Text, default="")
    permalink: Mapped[str] = mapped_column(Text, default="")
    thumbnail_url: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, index=True)
    like_count: Mapped[Optional[int]] = mapped_column(Integer)
    comments_count: Mapped[Optional[int]] = mapped_column(Integer)
    latest_insights: Mapped[dict] = mapped_column(JSON, default=dict)
    insights_captured_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    post_id: Mapped[Optional[int]] = mapped_column(ForeignKey("posts.id"))
    pillar: Mapped[str] = mapped_column(String(120), default="")


class MediaInsightSnapshot(Base):
    __tablename__ = "media_insight_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    media_record_id: Mapped[int] = mapped_column(ForeignKey("media_records.id"), index=True)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class AudienceSnapshot(Base):
    __tablename__ = "audience_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    metric: Mapped[str] = mapped_column(String(60))  # follower_demographics | engaged_audience_demographics
    breakdown: Mapped[str] = mapped_column(String(20))  # age | gender | country | city
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # {"US": 120, ...}
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ConversionMetric(Base):
    """Website/business outcomes imported from analytics with a stated measurement method."""

    __tablename__ = "conversion_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    day: Mapped[dt.date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(60))  # sessions | leads | enquiries | purchases | revenue
    value: Mapped[float] = mapped_column(Float)
    utm_campaign: Mapped[str] = mapped_column(String(120), default="")
    method: Mapped[str] = mapped_column(String(200), default="")  # e.g. "GA4 utm_source=instagram"
    source: Mapped[str] = mapped_column(String(60), default="csv_import")


# ------------------------------------------------------------------- growth
class Experiment(Base, TimestampMixin):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    variable: Mapped[str] = mapped_column(String(120))
    target_metric: Mapped[str] = mapped_column(String(60), default="reach")
    start_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    end_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    baseline: Mapped[Optional[float]] = mapped_column(Float)
    result: Mapped[Optional[float]] = mapped_column(Float)
    result_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(20), default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(20), default="")  # continue | stop | test_again
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned | running | evaluated
    origin: Mapped[str] = mapped_column(String(40), default="manual")  # manual | ai_hypothesis


# ---------------------------------------------------------------- community
class CommunityItem(Base, TimestampMixin):
    __tablename__ = "community_items"
    __table_args__ = (UniqueConstraint("account_id", "ig_comment_id", name="uq_comment_account"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ig_comment_id: Mapped[str] = mapped_column(String(64))
    ig_media_id: Mapped[str] = mapped_column(String(64), default="")
    username: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    commented_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    sentiment: Mapped[str] = mapped_column(String(20), default="")  # positive | neutral | negative
    category: Mapped[str] = mapped_column(String(30), default="")  # question | lead | complaint | spam | praise | other
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    classification_source: Mapped[str] = mapped_column(String(40), default="")
    draft_reply: Mapped[str] = mapped_column(Text, default="")
    reply_status: Mapped[str] = mapped_column(String(20), default="none")  # none|drafted|approved|sent|skipped|failed
    reply_error: Mapped[str] = mapped_column(Text, default="")
    replied_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class ReplyTemplate(Base, TimestampMixin):
    __tablename__ = "reply_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(30))  # which classified category it answers
    text: Mapped[str] = mapped_column(Text)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------------- reports & ops
class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # weekly | monthly | audit
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    workflow: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class AICache(Base):
    __tablename__ = "ai_cache"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IngestedFile(Base):
    """Every file path the watcher has handled, so unchanged files are not re-hashed each scan."""

    __tablename__ = "ingested_files"
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    asset_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assets.id"))
    outcome: Mapped[str] = mapped_column(String(30))  # new_asset | duplicate | rejected
    message: Mapped[str] = mapped_column(Text, default="")
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    """Alerts shown in the dashboard (and optionally forwarded to ALERT_WEBHOOK_URL)."""

    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warning | error
    account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(200), default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
