import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import select

from agency.analytics import audience, overview, sync_account
from agency.community import classify_rules
from agency.db import session_scope
from agency.models import Account, CommunityItem, MediaRecord, MetricAvailability, ReplyTemplate, Report, utcnow
from agency.reports import build_report, report_csv, report_pdf
from tests.helpers import create_account, drain

H = {"X-Requested-With": "agency"}


def _sync(acct_id):
    with session_scope() as s:
        return sync_account(s, s.get(Account, acct_id), backfill_days=3)


def test_sync_stores_real_values_and_flags_unavailable(env):
    acct = create_account()
    env.graph.media.append({"id": "m1", "caption": "Old post", "media_type": "IMAGE", "media_product_type": "FEED",
                            "permalink": "https://www.instagram.com/p/m1/", "timestamp": "2026-10-15T10:00:00+0000"})
    stats = _sync(acct)
    assert stats["profile"] and stats["media"] == 1 and stats["media_insights"] == 1
    with session_scope() as s:
        a = s.get(Account, acct)
        ov = overview(s, a, utcnow().date() - dt.timedelta(days=7), utcnow().date() + dt.timedelta(days=1))
        assert ov["followers"]["value"] == 1520
        assert ov["account_reach"]["value"] == 800 * 3  # three backfilled days, as returned
        assert ov["followers_gained"]["value"] == 36 and ov["followers_lost"]["value"] == 9
        assert ov["impressions"]["value"] is None and "v22" in ov["impressions"]["unavailable"]
        assert ov["profile_link_taps"]["value"] is None and ov["profile_link_taps"]["unavailable"]
        unavailable = {r.metric for r in s.scalars(select(MetricAvailability).where(MetricAvailability.available.is_(False)))}
        assert {"replies", "profile_links_taps"} <= unavailable
        rec = s.scalar(select(MediaRecord))
        assert rec.latest_insights["reach"] == 900 and "ig_reels_avg_watch_time" not in rec.latest_insights
        aud = audience(s, a)
        country = aud["follower_demographics"]["country"]
        assert country["available"] and list(country["data"])[0] == "US"
        city = aud["follower_demographics"]["city"]
        assert not city["available"] and "Data unavailable from connected source" in city["message"]
        assert "never inferred" in aud["policy"]


def test_empty_account_shows_no_fake_numbers(env):
    acct = create_account(connect=False)
    with session_scope() as s:
        a = s.get(Account, acct)
        ov = overview(s, a, utcnow().date() - dt.timedelta(days=30), utcnow().date())
        assert ov["followers"]["value"] is None
        assert ov["account_reach"]["value"] is None
        assert ov["content_published"]["value"] == 0
        assert not audience(s, a)["follower_demographics"]["country"]["available"]


def test_weekly_report_exports(env):
    acct = create_account()
    _sync(acct)
    with session_scope() as s:
        r = build_report(s, s.get(Account, acct), "weekly")
        csv_text, pdf = report_csv(r), report_pdf(r)
        assert r.data["narrative"] is None  # no AI configured: report says so instead of inventing text
    assert "summary,account_reach" in csv_text
    assert pdf[:4] == b"%PDF"


def test_monthly_report_includes_next_month_plan(env, fake_ai):
    acct = create_account()
    with session_scope() as s:
        r = build_report(s, s.get(Account, acct), "monthly")
        assert r.data["next_month_plan"]["strategy"]["campaign_themes"] == ["Lisbon"]
        assert r.period_end.day == 1


def test_comment_rules():
    assert classify_rules("Check my page for promo 💰💰 www.spam.biz")["category"] == "spam"
    assert classify_rules("I want a refund, this is a scam")["urgent"]
    assert classify_rules("How much is the print?")["category"] == "lead"
    assert classify_rules("This is stunning 😍")["category"] == "praise"


def test_community_escalates_and_auto_replies_only_low_risk(env):
    acct = create_account(mode="full", auto_reply_templates=True)
    env.graph.media.append({"id": "m9", "caption": "x", "media_type": "IMAGE", "media_product_type": "FEED",
                            "permalink": "p", "timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%S+0000")})
    env.graph.comments["m9"] = [
        {"id": "c1", "text": "Stunning shot 😍", "username": "fan", "timestamp": "2026-10-20T10:00:00+0000"},
        {"id": "c2", "text": "I was charged twice and want a refund", "username": "buyer", "timestamp": "2026-10-20T10:05:00+0000"},
    ]
    _sync(acct)
    with session_scope() as s:
        s.add(ReplyTemplate(account_id=acct, name="thanks", category="praise", text="Thank you so much!", auto_send=True))
        s.add(ReplyTemplate(account_id=acct, name="sorry", category="complaint", text="Sorry!", auto_send=True))
        from agency.jobs import enqueue

        enqueue(s, "sync_comments", {"account_id": acct})
    drain()
    with session_scope() as s:
        items = {c.ig_comment_id: c for c in s.scalars(select(CommunityItem))}
    assert items["c1"].reply_status == "sent"
    assert items["c2"].escalated and items["c2"].reply_status != "sent"
    assert env.graph.replies == [("c1", "Thank you so much!")]


def test_api_auth_csrf_roles_and_secret_hygiene(env):
    from agency.api.app import create_app

    client = TestClient(create_app())
    assert client.get("/api/session").json()["needs_setup"] is True
    assert client.post("/api/setup", json={"email": "o@x.com", "password": "longpassword1"}).status_code == 403  # no CSRF header
    assert client.post("/api/setup", json={"email": "o@x.com", "password": "longpassword1"}, headers=H).status_code == 200
    r = client.post("/api/accounts", json={"name": "Business Account", "account_type": "business"}, headers=H)
    assert r.status_code == 200
    acct_id = r.json()["id"]
    assert client.post("/api/users", json={"email": "v@x.com", "password": "viewerpass1", "role": "viewer"}, headers=H).status_code == 200
    integ = client.get("/api/integrations").json()
    assert integ["redirect_uri"] == "https://agency.example.com/api/integrations/instagram/callback"
    url = client.post(f"/api/accounts/{acct_id}/connect", headers=H).json()["authorize_url"]
    assert "instagram_business_content_publish" in url and "app-secret-xyz" not in url
    state = url.split("state=")[1].split("&")[0]
    cb = client.get(f"/api/integrations/instagram/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code in (302, 307) and "connected=1" in cb.headers["location"]
    acct = client.get(f"/api/accounts/{acct_id}").json()
    assert acct["connection_status"] == "connected" and acct["ig_username"] == "creator_demo"
    assert "LONG_TOKEN_SECRET_123" not in str(client.get("/api/integrations").json())
    # State cannot be replayed.
    replay = client.get(f"/api/integrations/instagram/callback?code=abc&state={state}", follow_redirects=False)
    assert "error" in replay.headers["location"]
    # Emergency pause through the API.
    assert client.post("/api/automation/pause", json={"paused": True}, headers=H).json()["publishing_paused"] is True
    # Viewer cannot change things.
    client.post("/api/logout", headers=H)
    client.post("/api/login", json={"email": "v@x.com", "password": "viewerpass1"}, headers=H)
    assert client.post("/api/automation/pause", json={"paused": False}, headers=H).status_code == 403
    assert client.get("/api/system").status_code == 200
    client.post("/api/logout", headers=H)
    assert client.get("/api/accounts").status_code == 401
    # Signed public media URLs reject tampering.
    assert client.get("/public-media/1.9999999999.deadbeef").status_code == 404


def test_audit_report_scores_transparently(env):
    from agency.audit_report import build_audit

    acct = create_account()
    env.graph.media.append({"id": "m1", "caption": "Old post", "media_type": "IMAGE", "media_product_type": "FEED",
                            "permalink": "https://www.instagram.com/p/m1/", "timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%S+0000")})
    _sync(acct)
    with session_scope() as s:
        rep = build_audit(s, s.get(Account, acct))
        health = rep.data["health"]
        assert health["score"] is not None
        assert all("method" in c for c in health["components"])
        assert any(w["item"] == "No link in bio" for w in rep.data["findings"]["weaknesses"])
        assert rep.data["competitors"]["status"] == "partially_supported"
        assert s.scalar(select(Report).where(Report.kind == "audit")) is not None
