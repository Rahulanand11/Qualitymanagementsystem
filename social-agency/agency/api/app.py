"""FastAPI application: JSON API, OAuth callback, public media endpoint, and the dashboard."""
from __future__ import annotations

import datetime as dt
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .. import analytics, community, connections, growth, instagram, pipeline, publishing, reports  # noqa: F401
from ..ai import usage_summary
from ..audit_report import build_audit
from ..config import get_settings
from ..db import get_session, init_db, table_names
from ..ingest import assign_asset, ensure_folders
from ..jobs import enqueue, retry_job
from ..media_probe import ffmpeg_available, media_kind
from ..models import (AUTONOMY_MODES, Account, Asset, AuditLog, BrandProfile, CommunityItem, ContentPlan, Derivative,
                      Experiment, Job, MediaInsightSnapshot, MediaRecord, MetricAvailability, Notification, OAuthState,
                      Post, PostMedia, ReplyTemplate, Report, User, utcnow)
from ..ops import audit, configure_logging, get_setting, publishing_globally_paused
from ..security import hash_password, verify_media_token, verify_password
from . import serializers as ser

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    init_db()
    app = FastAPI(title="AI Social Media Agency", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                       https_only=settings.is_production, max_age=14 * 86400, session_cookie="agency_session")

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Browsers cannot send this custom header cross-site without CORS approval, which we never grant.
        if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
                and request.headers.get("x-requested-with") != "agency"):
            return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    register_routes(app)
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html")
    return app


# ----------------------------------------------------------------- auth deps
def current_user(request: Request, s: Session = Depends(get_session)) -> User:
    uid = request.session.get("uid")
    user = s.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[role]:
            raise HTTPException(403, f"Requires {role} role")
        return user
    return dep


def get_account(s: Session, account_id: int) -> Account:
    a = s.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    return a


def _date(v: Optional[str], default: dt.date) -> dt.date:
    return dt.date.fromisoformat(v) if v else default


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "account"


# ------------------------------------------------------------------ schemas
class Credentials(BaseModel):
    email: str
    password: str
    setup_token: str = ""


class NewUser(Credentials):
    role: str = "editor"


class AccountIn(BaseModel):
    name: str
    account_type: str = "creator"
    folder_name: Optional[str] = None
    timezone: str = "UTC"
    posts_per_week: int = 4


class AccountPatch(BaseModel):
    name: Optional[str] = None
    autonomy_mode: Optional[str] = None
    publishing_paused: Optional[bool] = None
    posts_per_week: Optional[int] = None
    timezone: Optional[str] = None
    account_type: Optional[str] = None


class PostPatch(BaseModel):
    hook: Optional[str] = None
    caption: Optional[str] = None
    cta: Optional[str] = None
    hashtags: Optional[list[str]] = None
    keywords: Optional[list[str]] = None
    alt_text: Optional[str] = None
    cover_title: Optional[str] = None
    on_screen_text: Optional[str] = None
    pillar: Optional[str] = None
    objective: Optional[str] = None


class ScheduleIn(BaseModel):
    when: Optional[str] = None  # ISO datetime in UTC, or omitted for next best slot


class ExperimentIn(BaseModel):
    account_id: int
    hypothesis: str
    variable: str
    target_metric: str = "reach"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    baseline: Optional[float] = None
    origin: str = "manual"


ONBOARDING_QUESTIONS = [
    {"field": "ig_username", "q": "Instagram username and account type (creator or business)?"},
    {"field": "category/niche", "q": "Business or creator category and niche?"},
    {"field": "offers/positioning", "q": "Products, services, offers, or personal brand positioning?"},
    {"field": "target_audience", "q": "Target audience: countries, cities, age groups, other attributes?"},
    {"field": "objectives", "q": "Primary objectives (followers, reach, engagement, traffic, leads, sales, awareness)?"},
    {"field": "voice/visual_identity", "q": "Brand voice, colours, fonts, and creative references?"},
    {"field": "competitors/inspirations", "q": "Competitors and inspirational accounts?"},
    {"field": "website/ctas", "q": "Website, landing pages, product pages, preferred calls to action?"},
    {"field": "posts_per_week", "q": "Preferred publishing frequency and what content you have available?"},
    {"field": "markets/languages/priorities", "q": "Geographic markets, languages, and business priorities?"},
]


def register_routes(app: FastAPI) -> None:  # noqa: C901 - route table
    settings = get_settings()

    # ------------------------------------------------------------- health
    @app.get("/healthz", include_in_schema=False)
    def healthz(s: Session = Depends(get_session)):
        s.execute(select(1))
        return {"ok": True}

    # ------------------------------------------------------------- session
    @app.get("/api/session")
    def session_info(request: Request, s: Session = Depends(get_session)):
        needs_setup = s.scalar(select(func.count(User.id))) == 0
        uid = request.session.get("uid")
        user = s.get(User, uid) if uid else None
        return {"needs_setup": needs_setup, "setup_token_required": needs_setup and bool(settings.setup_token),
                "user": {"email": user.email, "role": user.role} if user else None,
                "publishing_paused": publishing_globally_paused(s), "warnings": settings.warnings}

    @app.post("/api/setup")
    def setup(body: Credentials, request: Request, s: Session = Depends(get_session)):
        if s.scalar(select(func.count(User.id))):
            raise HTTPException(409, "Already set up")
        # On a public URL, only someone who can read SETUP_TOKEN in the host's settings can claim the owner login.
        if settings.setup_token and not secrets.compare_digest(body.setup_token.strip(), settings.setup_token):
            raise HTTPException(403, "Wrong setup code (see SETUP_TOKEN in your hosting environment settings)")
        if len(body.password) < 10:
            raise HTTPException(400, "Use at least 10 characters")
        u = User(email=body.email.strip().lower(), password_hash=hash_password(body.password), role="owner")
        s.add(u)
        s.flush()
        request.session["uid"] = u.id
        audit(s, "user.setup", actor=u.email)
        return {"ok": True}

    @app.post("/api/login")
    def login(body: Credentials, request: Request, s: Session = Depends(get_session)):
        u = s.scalar(select(User).where(User.email == body.email.strip().lower()))
        if u is None or not verify_password(body.password, u.password_hash):
            audit(s, "user.login_failed", actor=body.email[:120])
            raise HTTPException(401, "Invalid email or password")
        request.session["uid"] = u.id
        audit(s, "user.login", actor=u.email)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/users")
    def users(s: Session = Depends(get_session), _: User = Depends(require("owner"))):
        return [{"id": u.id, "email": u.email, "role": u.role} for u in s.scalars(select(User))]

    @app.post("/api/users")
    def add_user(body: NewUser, s: Session = Depends(get_session), me: User = Depends(require("owner"))):
        if body.role not in ROLE_RANK:
            raise HTTPException(400, "Unknown role")
        s.add(User(email=body.email.strip().lower(), password_hash=hash_password(body.password), role=body.role))
        audit(s, "user.created", actor=me.email, role=body.role, email=body.email)
        return {"ok": True}

    # ------------------------------------------------------------ accounts
    @app.get("/api/accounts")
    def list_accounts(s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [ser.account(a) for a in s.scalars(select(Account).order_by(Account.id))]

    @app.post("/api/accounts")
    def create_account(body: AccountIn, s: Session = Depends(get_session), me: User = Depends(require("owner"))):
        slug = slugify(body.name)
        if s.scalar(select(Account).where(Account.slug == slug)):
            raise HTTPException(409, "An account with this name exists")
        a = Account(name=body.name, slug=slug, account_type=body.account_type, timezone=body.timezone,
                    posts_per_week=body.posts_per_week, folder_name=body.folder_name or body.name)
        a.brand = BrandProfile(content_pillars=[], automation_rules={"min_qc_score": 85, "auto_formats": ["image", "carousel"],
                                                                     "subtitles": True})
        s.add(a)
        s.flush()
        ensure_folders(s)
        audit(s, "account.created", actor=me.email, account_id=a.id, entity_type="account", entity_id=a.id)
        return ser.account(a)

    @app.get("/api/accounts/{account_id}")
    def get_acct(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = get_account(s, account_id)
        return {**ser.account(a), "profile": a.profile_cache, "brand": ser.brand(a.brand),
                "inbox_path": str(settings.watch_root / a.folder_name / "Inbox")}

    @app.patch("/api/accounts/{account_id}")
    def patch_acct(account_id: int, body: AccountPatch, s: Session = Depends(get_session),
                   me: User = Depends(require("editor"))):
        a = get_account(s, account_id)
        changes = body.model_dump(exclude_none=True)
        if "autonomy_mode" in changes:
            if changes["autonomy_mode"] not in AUTONOMY_MODES:
                raise HTTPException(400, f"autonomy_mode must be one of {AUTONOMY_MODES}")
            if me.role != "owner":
                raise HTTPException(403, "Only the owner can change autonomy")
        for k, v in changes.items():
            setattr(a, k, v)
        audit(s, "account.updated", actor=me.email, account_id=a.id, entity_type="account", entity_id=a.id, **changes)
        return ser.account(a)

    @app.get("/api/onboarding/questions")
    def onboarding_questions(_: User = Depends(current_user)):
        return ONBOARDING_QUESTIONS

    @app.put("/api/accounts/{account_id}/brand")
    def put_brand(account_id: int, body: dict[str, Any], s: Session = Depends(get_session),
                  me: User = Depends(require("editor"))):
        a = get_account(s, account_id)
        for k, v in body.items():
            if k in ser.BRAND_FIELDS:
                setattr(a.brand, k, v)
        audit(s, "brand.updated", actor=me.email, account_id=a.id, entity_type="brand", entity_id=a.id,
              fields=sorted(k for k in body if k in ser.BRAND_FIELDS))
        return ser.brand(a.brand)

    # -------------------------------------------------------- integrations
    @app.get("/api/integrations")
    def integrations(s: Session = Depends(get_session), _: User = Depends(current_user)):
        accts = []
        for a in s.scalars(select(Account)):
            cred = connections.active_credential(s, a)
            accts.append({**ser.account(a), "token_expires_at": ser.iso(cred.token_expires_at) if cred else None,
                          "scopes": cred.scopes if cred else []})
        return {"meta_configured": settings.meta_configured, "redirect_uri": instagram.redirect_uri(),
                "public_media_ready": settings.public_media_ready, "public_base_url": settings.public_base_url,
                "anthropic_configured": bool(settings.anthropic_api_key), "ai_model": settings.ai_model,
                "ffmpeg": ffmpeg_available(), "watch_root": str(settings.watch_root),
                "subtitles_engine": _has_module("faster_whisper"), "accounts": accts,
                "scopes_requested": instagram.SCOPES}

    @app.post("/api/accounts/{account_id}/connect")
    def connect(account_id: int, s: Session = Depends(get_session), me: User = Depends(require("owner"))):
        a = get_account(s, account_id)
        if not settings.meta_configured:
            raise HTTPException(400, "Set META_APP_ID and META_APP_SECRET first (see Integrations page)")
        state = secrets.token_urlsafe(32)
        s.add(OAuthState(state=state, account_id=a.id))
        audit(s, "integration.connect_started", actor=me.email, account_id=a.id)
        return {"authorize_url": instagram.authorize_url(state)}

    @app.get("/api/integrations/instagram/callback", include_in_schema=False)
    def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "",
                       error_description: str = "", s: Session = Depends(get_session)):
        if "uid" not in request.session:
            return RedirectResponse("/#/integrations?error=Sign+in+before+connecting")
        st = s.get(OAuthState, state) if state else None
        if st is None or utcnow() - st.created_at > dt.timedelta(minutes=15):
            return RedirectResponse("/#/integrations?error=Invalid+or+expired+authorization+state")
        account = s.get(Account, st.account_id)
        s.delete(st)
        if error or not code:
            audit(s, "integration.connect_denied", account_id=account.id, error=error)
            return RedirectResponse(f"/#/integrations?error={error_description or error or 'Authorization+cancelled'}")
        try:
            tok = instagram.exchange_code(code)
            client = instagram.InstagramClient(tok["access_token"], tok["user_id"] or "me")
            with client:
                profile = client.profile()
            if account.ig_user_id and profile.get("user_id") and str(profile["user_id"]) != account.ig_user_id:
                return RedirectResponse("/#/integrations?error=This+Instagram+login+is+a+different+account+than+before")
            connections.store_token(s, account, tok["access_token"], tok["expires_in"], tok["permissions"],
                                    str(profile.get("user_id") or tok["user_id"]))
            account.ig_username = profile.get("username")
            account.profile_cache = profile
            if profile.get("account_type"):
                account.account_type = "business" if profile["account_type"] == "BUSINESS" else "creator"
            enqueue(s, "sync_account", {"account_id": account.id}, account_id=account.id,
                    idempotency_key=f"sync:{account.id}:connect:{utcnow():%Y%m%d%H%M}")
        except instagram.InstagramError as exc:
            account.connection_status, account.connection_error = "error", str(exc)
            audit(s, "integration.connect_failed", account_id=account.id, error=str(exc))
            return RedirectResponse("/#/integrations?error=Instagram+connection+failed")
        return RedirectResponse("/#/integrations?connected=1")

    @app.post("/api/accounts/{account_id}/disconnect")
    def disconnect(account_id: int, s: Session = Depends(get_session), me: User = Depends(require("owner"))):
        connections.revoke(s, get_account(s, account_id), me.email)
        return {"ok": True, "note": "Also remove the app in Instagram → Settings → Apps and websites to revoke on Meta's side."}

    @app.post("/api/accounts/{account_id}/sync")
    def sync_now(account_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        a = get_account(s, account_id)
        if a.connection_status != "connected":
            raise HTTPException(400, "Account is not connected")
        j = enqueue(s, "sync_account", {"account_id": a.id}, account_id=a.id,
                    idempotency_key=f"sync:{a.id}:manual:{utcnow():%Y%m%d%H%M}")
        enqueue(s, "sync_comments", {"account_id": a.id}, account_id=a.id,
                idempotency_key=f"comments:{a.id}:manual:{utcnow():%Y%m%d%H%M}")
        return {"job_id": j.id}

    # -------------------------------------------------------------- assets
    @app.get("/api/assets")
    def list_assets(account_id: Optional[int] = None, status: Optional[str] = None, q: str = "",
                    unassigned: bool = False, limit: int = 200, s: Session = Depends(get_session),
                    _: User = Depends(current_user)):
        query = select(Asset).order_by(Asset.created_at.desc()).limit(min(limit, 500))
        if account_id:
            query = query.where(Asset.account_id == account_id)
        if unassigned:
            query = query.where(Asset.account_id.is_(None))
        if status:
            query = query.where(Asset.status == status)
        if q:
            query = query.where(Asset.filename.ilike(f"%{q}%") | Asset.campaign.ilike(f"%{q}%"))
        return [ser.asset(a) for a in s.scalars(query)]

    @app.get("/api/assets/{asset_id}")
    def get_asset(asset_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = s.get(Asset, asset_id) or _404()
        events = s.scalars(select(AuditLog).where(AuditLog.entity_type == "asset", AuditLog.entity_id == a.id)
                           .order_by(AuditLog.ts)).all()
        return {**ser.asset(a, full=True), "events": [{"ts": ser.iso(e.ts), "action": e.action, "detail": e.detail}
                                                      for e in events]}

    @app.post("/api/assets/{asset_id}/assign")
    def assign(asset_id: int, body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        a = s.get(Asset, asset_id) or _404()
        assign_asset(s, a, get_account(s, int(body["account_id"])), me.email, body.get("campaign"))
        return ser.asset(a)

    @app.post("/api/assets/{asset_id}/rights")
    def rights(asset_id: int, body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        a = s.get(Asset, asset_id) or _404()
        a.rights_confirmed = bool(body.get("confirmed"))
        a.rights_note = f"{body.get('note', '')} (confirmed by {me.email})" if a.rights_confirmed else ""
        audit(s, "asset.rights_updated", actor=me.email, account_id=a.account_id, entity_type="asset", entity_id=a.id,
              confirmed=a.rights_confirmed)
        for pm_post in s.scalars(select(Post).where(Post.media.any(PostMedia.asset_id == a.id),
                                                    Post.status.in_(("draft", "needs_review")))):
            publishing.refresh_review_state(s, pm_post, s.get(Account, pm_post.account_id))
        return ser.asset(a)

    @app.post("/api/assets/{asset_id}/reprocess")
    def reprocess(asset_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        a = s.get(Asset, asset_id) or _404()
        if a.account_id is None:
            raise HTTPException(400, "Assign the asset to an account first")
        a.status, a.status_reason, a.analysis = "new", "", {}
        enqueue(s, "analyze_asset", {"asset_id": a.id}, account_id=a.account_id,
                idempotency_key=f"analyze:{a.id}:{utcnow():%Y%m%d%H%M%S}")
        s.flush()
        for kind in ("edit", ):
            j = s.scalar(select(Job).where(Job.idempotency_key == f"{kind}:{a.id}"))
            if j:
                j.idempotency_key = None
        audit(s, "asset.reprocess", actor=me.email, account_id=a.account_id, entity_type="asset", entity_id=a.id)
        return ser.asset(a)

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), account_id: Optional[int] = Form(None), campaign: str = Form(""),
                     s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        name = Path(file.filename or "upload").name
        if media_kind(Path(name)) is None:
            raise HTTPException(400, "Unsupported file type")
        root = ensure_folders(s)
        if account_id:
            target_dir = root / get_account(s, account_id).folder_name / "Inbox"
        else:
            target_dir = root / "Shared Assets"
        if campaign:
            target_dir = target_dir / re.sub(r"[^\w\- ]", "", campaign)[:80]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        i = 1
        while target.exists():
            target = target_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        tmp = target.with_suffix(target.suffix + ".part")  # watcher ignores .part until renamed
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        tmp.rename(target)
        audit(s, "asset.uploaded", actor=me.email, account_id=account_id, path=str(target.relative_to(root)))
        return {"ok": True, "path": str(target.relative_to(root)),
                "note": "The worker picks the file up on its next scan."}

    @app.get("/api/media/{derivative_id}")
    def media(derivative_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        d = s.get(Derivative, derivative_id) or _404()
        return FileResponse(d.path)

    @app.get("/api/assets/{asset_id}/original")
    def original(asset_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = s.get(Asset, asset_id) or _404()
        return FileResponse(a.original_path, filename=a.filename)

    @app.get("/public-media/{token}", include_in_schema=False)
    def public_media(token: str, s: Session = Depends(get_session)):
        did = verify_media_token(token)
        d = s.get(Derivative, did) if did else None
        if d is None:
            raise HTTPException(404)
        media_type = "image/jpeg" if d.path.endswith(".jpg") else "video/mp4"
        return FileResponse(d.path, media_type=media_type)

    # --------------------------------------------------------------- posts
    @app.get("/api/posts")
    def list_posts(account_id: Optional[int] = None, status: Optional[str] = None, limit: int = 200,
                   s: Session = Depends(get_session), _: User = Depends(current_user)):
        q = select(Post).order_by(Post.created_at.desc()).limit(min(limit, 500))
        if account_id:
            q = q.where(Post.account_id == account_id)
        if status:
            q = q.where(Post.status.in_(status.split(",")))
        return [ser.post(p) for p in s.scalars(q)]

    @app.get("/api/posts/{post_id}")
    def get_post(post_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        p = s.get(Post, post_id) or _404()
        events = s.scalars(select(AuditLog).where(AuditLog.entity_type == "post", AuditLog.entity_id == p.id)
                           .order_by(AuditLog.ts)).all()
        return {**ser.post(p, full=True), "full_caption": publishing.full_caption(p),
                "events": [{"ts": ser.iso(e.ts), "action": e.action, "actor": e.actor, "detail": e.detail} for e in events]}

    @app.patch("/api/posts/{post_id}")
    def patch_post(post_id: int, body: PostPatch, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        if p.status in ("publishing", "published"):
            raise HTTPException(400, "Published posts cannot be edited here")
        changes = body.model_dump(exclude_none=True)
        for k, v in changes.items():
            setattr(p, k, v)
        if changes:
            p.content_source = p.content_source if p.content_source.startswith("ai:") else "manual"
            if p.content_source == "manual":
                p.risk_flags = [f for f in (p.risk_flags or []) if not f.startswith("Draft copy only")]
            p.approved_by = None
            if p.status == "scheduled":
                p.status = "needs_review"
            publishing.refresh_review_state(s, p, s.get(Account, p.account_id))
        audit(s, "post.edited", actor=me.email, account_id=p.account_id, entity_type="post", entity_id=p.id,
              fields=sorted(changes))
        return ser.post(p, full=True)

    @app.post("/api/posts/{post_id}/variant/{index}")
    def choose_variant(post_id: int, index: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        if not 0 <= index < len(p.variants or []):
            raise HTTPException(400, "No such variant")
        v = p.variants[index]
        p.hook, p.cta, p.hashtags, p.keywords = v["hook"], v["cta"], v["hashtags"], v["keywords"]
        p.caption = f"{v['hook']}\n\n{v['caption']}" + (f"\n\n{v['cta']}" if v["cta"] else "")
        p.alt_text, p.cover_title, p.on_screen_text = v["alt_text"], v["cover_title"], v["on_screen_text"]
        p.approved_by = None
        publishing.refresh_review_state(s, p, s.get(Account, p.account_id))
        audit(s, "post.variant_selected", actor=me.email, account_id=p.account_id, entity_type="post", entity_id=p.id,
              index=index)
        return ser.post(p, full=True)

    @app.post("/api/posts/{post_id}/approve")
    def approve(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        try:
            publishing.approve_post(s, p, me.email)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return ser.post(p)

    @app.post("/api/posts/{post_id}/reject")
    def reject(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        if p.status in ("published", "publishing"):
            raise HTTPException(400, "Cannot cancel a published post")
        p.status, p.scheduled_for = "cancelled", None
        audit(s, "post.cancelled", actor=me.email, account_id=p.account_id, entity_type="post", entity_id=p.id)
        return ser.post(p)

    @app.post("/api/posts/{post_id}/schedule")
    def schedule(post_id: int, body: ScheduleIn, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        account = s.get(Account, p.account_id)
        from ..planner import next_slot

        when = dt.datetime.fromisoformat(body.when.replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None) \
            if body.when else next_slot(s, account)
        try:
            publishing.schedule_post(s, p, when, "manual" if body.when else "best_slot", actor=me.email)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return ser.post(p)

    @app.post("/api/posts/{post_id}/publish-now")
    def publish_now(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        if p.status != "approved" and p.status != "scheduled":
            raise HTTPException(400, "Approve the post first")
        p.status, p.scheduled_for, p.schedule_source = "scheduled", utcnow(), "publish_now"
        audit(s, "post.publish_now", actor=me.email, account_id=p.account_id, entity_type="post", entity_id=p.id)
        return ser.post(p)

    @app.post("/api/posts/{post_id}/unschedule")
    def unschedule(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        if p.status != "scheduled":
            raise HTTPException(400, "Post is not scheduled")
        p.status, p.scheduled_for = "approved", None
        audit(s, "post.unscheduled", actor=me.email, account_id=p.account_id, entity_type="post", entity_id=p.id)
        return ser.post(p)

    @app.post("/api/posts/{post_id}/retry")
    def retry(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        try:
            publishing.retry_failed_post(s, p, me.email)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return ser.post(p)

    @app.post("/api/posts/{post_id}/regenerate")
    def regenerate(post_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        pipeline.regenerate_post_copy(s, p, me.email)
        return ser.post(p, full=True)

    @app.post("/api/posts/{post_id}/experiment")
    def tag_experiment(post_id: int, body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        p = s.get(Post, post_id) or _404()
        p.experiment_id, p.experiment_arm = body.get("experiment_id"), body.get("arm", "test")
        return ser.post(p)

    # ------------------------------------------------------------ calendar
    @app.get("/api/calendar")
    def calendar(account_id: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None,
                 s: Session = Depends(get_session), _: User = Depends(current_user)):
        st = dt.datetime.combine(_date(start, utcnow().date() - dt.timedelta(days=7)), dt.time())
        en = dt.datetime.combine(_date(end, st.date() + dt.timedelta(days=42)), dt.time())
        q = select(Post).where(((Post.scheduled_for >= st) & (Post.scheduled_for < en)) |
                               ((Post.published_at >= st) & (Post.published_at < en)))
        if account_id:
            q = q.where(Post.account_id == account_id)
        return [ser.post(p) for p in s.scalars(q)]

    @app.post("/api/plans/{kind}")
    def make_plan(kind: str, body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        from ..planner import monthly_plan, weekly_plan

        a = get_account(s, int(body["account_id"]))
        start = dt.date.fromisoformat(body["start"]) if body.get("start") else None
        plan = weekly_plan(s, a, start) if kind == "weekly" else monthly_plan(s, a, start)
        s.flush()
        return {"id": plan.id, "period_type": plan.period_type, "start_date": str(plan.start_date),
                "source": plan.source, "plan": plan.plan}

    @app.get("/api/plans")
    def plans(account_id: int, kind: str = "week", s: Session = Depends(get_session), _: User = Depends(current_user)):
        rows = s.scalars(select(ContentPlan).where(ContentPlan.account_id == account_id, ContentPlan.period_type == kind)
                         .order_by(ContentPlan.created_at.desc()).limit(10))
        return [{"id": p.id, "start_date": str(p.start_date), "source": p.source, "plan": p.plan,
                 "created_at": ser.iso(p.created_at)} for p in rows]

    @app.post("/api/plans/{plan_id}/apply")
    def apply_plan(plan_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        from ..planner import apply_weekly_plan

        plan = s.get(ContentPlan, plan_id) or _404()
        try:
            return {"scheduled": apply_weekly_plan(s, plan, me.email)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # ----------------------------------------------------------- analytics
    @app.get("/api/analytics/overview")
    def an_overview(account_id: int, start: Optional[str] = None, end: Optional[str] = None,
                    s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = get_account(s, account_id)
        en = _date(end, utcnow().date() + dt.timedelta(days=1))
        st = _date(start, en - dt.timedelta(days=30))
        cur = analytics.overview(s, a, st, en)
        span = en - st
        prev = analytics.overview(s, a, st - span, st)
        return {"current": cur, "previous": {k: v for k, v in prev.items() if k not in ("definitions", "best_content")}}

    @app.get("/api/analytics/series")
    def an_series(account_id: int, metric: str, start: Optional[str] = None, end: Optional[str] = None,
                  s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = get_account(s, account_id)
        en = _date(end, utcnow().date() + dt.timedelta(days=1))
        return analytics.daily_series(s, a, metric, _date(start, en - dt.timedelta(days=90)), en)

    @app.get("/api/analytics/content")
    def an_content(account_id: int, start: Optional[str] = None, end: Optional[str] = None,
                   s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = get_account(s, account_id)
        st = dt.datetime.combine(_date(start, dt.date(2000, 1, 1)), dt.time())
        en = dt.datetime.combine(_date(end, utcnow().date() + dt.timedelta(days=1)), dt.time())
        return analytics.media_rows(s, a, st, en)

    @app.get("/api/analytics/content/{record_id}")
    def an_content_item(record_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        rec = s.get(MediaRecord, record_id) or _404()
        snaps = s.scalars(select(MediaInsightSnapshot).where(MediaInsightSnapshot.media_record_id == rec.id)
                          .order_by(MediaInsightSnapshot.captured_at))
        return {"id": rec.id, "permalink": rec.permalink, "caption": rec.caption, "posted_at": ser.iso(rec.posted_at),
                "format": analytics.fmt_of(rec), "post_id": rec.post_id,
                "history": [{"captured_at": ser.iso(x.captured_at), "metrics": x.metrics} for x in snaps]}

    @app.get("/api/analytics/audience")
    def an_audience(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        return analytics.audience(s, get_account(s, account_id))

    @app.get("/api/analytics/availability")
    def an_avail(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [{"scope": r.scope, "metric": r.metric, "available": r.available, "message": r.message,
                 "checked_at": ser.iso(r.checked_at)}
                for r in s.scalars(select(MetricAvailability).where(MetricAvailability.account_id == account_id))]

    @app.get("/api/analytics/growth")
    def an_growth(account_id: int, days: int = 7, s: Session = Depends(get_session), _: User = Depends(current_user)):
        a = get_account(s, account_id)
        return {"insights": growth.growth_insights(s, a, days), "targets": growth.target_tracking(s, a),
                "performance": growth.performance_summary(s, a, 90), "pillar_adjustments": growth.pillar_adjustments(s, a)}

    @app.post("/api/analytics/conversions")
    async def an_conversions(account_id: int = Form(...), method: str = Form(...), file: UploadFile = File(...),
                             s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        if not method.strip():
            raise HTTPException(400, "Describe the attribution method (e.g. 'GA4, utm_source=instagram')")
        text = (await file.read()).decode("utf-8-sig")
        return {"rows": analytics.import_conversions_csv(s, get_account(s, account_id), text, method)}

    @app.get("/api/analytics/definitions")
    def an_defs(_: User = Depends(current_user)):
        return analytics.METRIC_DEFINITIONS

    # --------------------------------------------------------------- audit
    @app.post("/api/audit/{account_id}")
    def run_audit(account_id: int, s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        r = build_audit(s, get_account(s, account_id))
        s.flush()
        return {"id": r.id, "data": r.data}

    @app.get("/api/audit/{account_id}")
    def latest_audit(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        r = s.scalar(select(Report).where(Report.account_id == account_id, Report.kind == "audit")
                     .order_by(Report.created_at.desc()))
        return {"id": r.id, "created_at": ser.iso(r.created_at), "data": r.data} if r else None

    # --------------------------------------------------------- experiments
    @app.get("/api/experiments")
    def list_exps(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [ser.experiment(e) for e in s.scalars(select(Experiment).where(Experiment.account_id == account_id)
                                                     .order_by(Experiment.created_at.desc()))]

    @app.post("/api/experiments")
    def create_exp(body: ExperimentIn, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        e = Experiment(account_id=body.account_id, hypothesis=body.hypothesis, variable=body.variable,
                       target_metric=body.target_metric, baseline=body.baseline, origin=body.origin,
                       start_date=dt.date.fromisoformat(body.start_date) if body.start_date else utcnow().date(),
                       end_date=dt.date.fromisoformat(body.end_date) if body.end_date else None, status="running")
        s.add(e)
        s.flush()
        audit(s, "experiment.created", actor=me.email, account_id=e.account_id, entity_type="experiment", entity_id=e.id)
        return ser.experiment(e)

    @app.patch("/api/experiments/{exp_id}")
    def patch_exp(exp_id: int, body: dict, s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        e = s.get(Experiment, exp_id) or _404()
        for k in ("hypothesis", "variable", "target_metric", "decision", "status", "limitations"):
            if k in body:
                setattr(e, k, body[k])
        return ser.experiment(e)

    @app.post("/api/experiments/{exp_id}/evaluate")
    def eval_exp(exp_id: int, s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        return ser.experiment(growth.evaluate_experiment(s, s.get(Experiment, exp_id) or _404()))

    # ----------------------------------------------------------- community
    @app.get("/api/community")
    def list_comments(account_id: int, category: Optional[str] = None, s: Session = Depends(get_session),
                      _: User = Depends(current_user)):
        q = select(CommunityItem).where(CommunityItem.account_id == account_id).order_by(CommunityItem.commented_at.desc()).limit(300)
        if category:
            q = q.where(CommunityItem.category == category)
        return {"items": [ser.comment(c) for c in s.scalars(q)],
                "summary": community.weekly_summary(s, get_account(s, account_id))}

    @app.post("/api/community/{item_id}/reply")
    def reply(item_id: int, body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        c = s.get(CommunityItem, item_id) or _404()
        try:
            community.send_reply(s, s.get(Account, c.account_id), c, me.email, body.get("text"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return ser.comment(c)

    @app.post("/api/community/{item_id}/skip")
    def skip(item_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        c = s.get(CommunityItem, item_id) or _404()
        c.reply_status = "skipped"
        return ser.comment(c)

    @app.get("/api/reply-templates")
    def templates(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [{"id": t.id, "name": t.name, "category": t.category, "text": t.text, "auto_send": t.auto_send}
                for t in s.scalars(select(ReplyTemplate).where(ReplyTemplate.account_id == account_id))]

    @app.post("/api/reply-templates")
    def add_template(body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        if body.get("auto_send") and body.get("category") not in community.AUTO_SEND_CATEGORIES:
            raise HTTPException(400, f"Auto-send is only allowed for {sorted(community.AUTO_SEND_CATEGORIES)}")
        t = ReplyTemplate(account_id=int(body["account_id"]), name=body["name"], category=body["category"],
                          text=body["text"], auto_send=bool(body.get("auto_send")))
        s.add(t)
        audit(s, "template.created", actor=me.email, account_id=t.account_id, auto_send=t.auto_send)
        return {"ok": True}

    @app.delete("/api/reply-templates/{tid}")
    def del_template(tid: int, s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        t = s.get(ReplyTemplate, tid) or _404()
        s.delete(t)
        return {"ok": True}

    # ------------------------------------------------------------- reports
    @app.get("/api/reports")
    def list_reports(account_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [{"id": r.id, "kind": r.kind, "period_start": str(r.period_start), "period_end": str(r.period_end),
                 "created_at": ser.iso(r.created_at)}
                for r in s.scalars(select(Report).where(Report.account_id == account_id).order_by(Report.created_at.desc()))]

    @app.post("/api/reports")
    def create_report(body: dict, s: Session = Depends(get_session), _: User = Depends(require("editor"))):
        if body.get("kind") not in ("weekly", "monthly"):
            raise HTTPException(400, "kind must be weekly or monthly")
        r = reports.build_report(s, get_account(s, int(body["account_id"])), body["kind"])
        return {"id": r.id}

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        r = s.get(Report, report_id) or _404()
        return {"id": r.id, "kind": r.kind, "data": r.data, "created_at": ser.iso(r.created_at)}

    @app.get("/api/reports/{report_id}/export.csv")
    def report_csv(report_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        r = s.get(Report, report_id) or _404()
        return PlainTextResponse(reports.report_csv(r), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="report_{r.id}.csv"'})

    @app.get("/api/reports/{report_id}/export.pdf")
    def report_pdf(report_id: int, s: Session = Depends(get_session), _: User = Depends(current_user)):
        r = s.get(Report, report_id) or _404()
        if r.kind == "audit":
            raise HTTPException(400, "Use the Account Audit page for audits")
        return Response(reports.report_pdf(r), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="report_{r.id}.pdf"'})

    # ---------------------------------------------------------- automation
    @app.get("/api/automation")
    def automation(s: Session = Depends(get_session), _: User = Depends(current_user)):
        return {"publishing_paused": publishing_globally_paused(s), "modes": AUTONOMY_MODES,
                "accounts": [{**ser.account(a), "rules": a.brand.automation_rules} for a in s.scalars(select(Account))]}

    @app.post("/api/automation/pause")
    def pause(body: dict, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        publishing.set_global_pause(s, bool(body.get("paused", True)), me.email)
        if body.get("paused", True):
            # Stop queued publish jobs immediately; they are re-queued by the scheduler after resume.
            for j in s.scalars(select(Job).where(Job.kind == "publish_post", Job.status == "queued")):
                j.status, j.idempotency_key, j.last_error = "dead", None, "Cancelled by emergency pause"
                post = s.get(Post, j.payload["post_id"])
                if post and post.status == "publishing" and not post.ig_container_id:
                    post.status = "scheduled"
        return {"publishing_paused": publishing_globally_paused(s)}

    # -------------------------------------------------------------- system
    @app.get("/api/system")
    def system(s: Session = Depends(get_session), _: User = Depends(current_user)):
        counts = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        failed = s.scalars(select(Job).where(Job.status == "dead").order_by(Job.updated_at.desc()).limit(50)).all()
        queued = s.scalars(select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.run_after).limit(50)).all()
        hb = get_setting(s, "worker_heartbeat")
        hb_age = (utcnow() - dt.datetime.fromisoformat(hb)).total_seconds() if hb else None
        storage = sum(f.stat().st_size for f in settings.storage_dir.rglob("*") if f.is_file())
        return {"database": {"ok": True, "tables": len(table_names()), "url_kind": settings.database_url.split(":")[0]},
                "worker": {"last_heartbeat": hb, "seconds_ago": hb_age,
                           "healthy": hb_age is not None and hb_age < 5 * max(settings.worker_interval_seconds, 15)},
                "jobs": counts, "failed_jobs": [ser.job(j) for j in failed], "queue": [ser.job(j) for j in queued],
                "storage_bytes": storage, "disk_free_bytes": shutil.disk_usage(settings.data_dir).free,
                "ai": usage_summary(s), "ffmpeg": ffmpeg_available(),
                "assets": dict(s.execute(select(Asset.status, func.count()).group_by(Asset.status)).all()),
                "posts": dict(s.execute(select(Post.status, func.count()).group_by(Post.status)).all()),
                "environment": settings.app_env, "warnings": settings.warnings}

    @app.post("/api/jobs/{job_id}/retry")
    def job_retry(job_id: int, s: Session = Depends(get_session), me: User = Depends(require("editor"))):
        j = s.get(Job, job_id) or _404()
        retry_job(s, j)
        audit(s, "job.retried", actor=me.email, entity_type="job", entity_id=j.id)
        return ser.job(j)

    @app.get("/api/notifications")
    def notes(s: Session = Depends(get_session), _: User = Depends(current_user)):
        return [{"id": n.id, "ts": ser.iso(n.ts), "level": n.level, "title": n.title, "message": n.message,
                 "link": n.link, "read": n.read, "account_id": n.account_id}
                for n in s.scalars(select(Notification).order_by(Notification.ts.desc()).limit(100))]

    @app.post("/api/notifications/read")
    def notes_read(s: Session = Depends(get_session), _: User = Depends(current_user)):
        for n in s.scalars(select(Notification).where(Notification.read.is_(False))):
            n.read = True
        return {"ok": True}

    @app.get("/api/audit-log")
    def audit_log(account_id: Optional[int] = None, limit: int = 200, s: Session = Depends(get_session),
                  _: User = Depends(current_user)):
        q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(min(limit, 1000))
        if account_id:
            q = q.where(AuditLog.account_id == account_id)
        return [{"ts": ser.iso(e.ts), "actor": e.actor, "action": e.action, "account_id": e.account_id,
                 "entity": f"{e.entity_type}#{e.entity_id}" if e.entity_type else "", "detail": e.detail}
                for e in s.scalars(q)]


def _404():
    raise HTTPException(404, "Not found")


def _has_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
