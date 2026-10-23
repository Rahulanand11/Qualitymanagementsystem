"""Instagram API with Instagram Login (graph.instagram.com) client.

Covers OAuth (authorize URL, code exchange, long-lived token, refresh), profile, media,
insights (with per-metric fallback so one unsupported metric never hides the rest),
demographics, content publishing (image, carousel, reels; container status polling;
publishing-limit checks), and comments.

Access tokens are passed only in request parameters and are scrubbed from every error
message. Docs: https://developers.facebook.com/docs/instagram-platform
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from .config import get_settings
from .security import scrub

log = logging.getLogger("agency.instagram")

GRAPH = "https://graph.instagram.com"
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_insights",
    "instagram_business_manage_comments",
]
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
TRANSIENT_CODES = {1, 2, -1}
AUTH_CODES = {190, 102, 10, 200}

# Account-level metrics requested as daily totals. Unsupported ones are recorded as unavailable.
ACCOUNT_TOTAL_METRICS = ["reach", "views", "accounts_engaged", "total_interactions", "likes", "comments",
                         "shares", "saves", "replies", "profile_links_taps"]
ACCOUNT_TIME_SERIES_METRICS = ["reach", "follower_count"]
MEDIA_METRICS_COMMON = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions"]
MEDIA_METRICS_FEED = ["profile_visits", "follows"]
MEDIA_METRICS_REELS = ["ig_reels_avg_watch_time", "ig_reels_video_view_total_time"]
DEMOGRAPHIC_BREAKDOWNS = ["country", "city", "age", "gender"]


class InstagramError(Exception):
    def __init__(self, message: str, *, code: int | None = None, status: int | None = None,
                 kind: str = "error", retry_after: int | None = None):
        super().__init__(scrub(message))
        self.code, self.status, self.kind, self.retry_after = code, status, kind, retry_after


# Tests replace this to route requests to a fake Graph API.
transport_factory: Callable[[], httpx.BaseTransport | None] = lambda: None


def _http() -> httpx.Client:
    return httpx.Client(transport=transport_factory(), timeout=60)


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code")
    msg = err.get("error_user_msg") or err.get("message") or resp.text[:300]
    if resp.status_code == 429 or code in RATE_LIMIT_CODES:
        raise InstagramError(f"Rate limited: {msg}", code=code, status=resp.status_code, kind="rate_limit",
                             retry_after=int(resp.headers.get("Retry-After", 900)))
    if code in AUTH_CODES or resp.status_code == 401:
        raise InstagramError(f"Authorization problem: {msg}", code=code, status=resp.status_code, kind="auth")
    if resp.status_code >= 500 or code in TRANSIENT_CODES or err.get("is_transient"):
        raise InstagramError(f"Temporary Instagram error: {msg}", code=code, status=resp.status_code, kind="transient")
    raise InstagramError(f"Instagram API error: {msg}", code=code, status=resp.status_code, kind="error")


# ------------------------------------------------------------------- OAuth
def redirect_uri() -> str:
    return f"{get_settings().public_base_url}/api/integrations/instagram/callback"


def authorize_url(state: str) -> str:
    st = get_settings()
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": st.meta_app_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": ",".join(SCOPES), "state": state, "enable_fb_login": "0",
    })


def exchange_code(code: str) -> dict:
    """Code -> short-lived token -> long-lived (60 day) token."""
    st = get_settings()
    with _http() as http:
        r = http.post(TOKEN_URL, data={"client_id": st.meta_app_id, "client_secret": st.meta_app_secret,
                                        "grant_type": "authorization_code", "redirect_uri": redirect_uri(),
                                        "code": code})
        _raise_for(r)
        short = r.json()
        if "data" in short and isinstance(short["data"], list):
            short = short["data"][0]
        r = http.get(f"{GRAPH}/access_token", params={"grant_type": "ig_exchange_token",
                                                      "client_secret": st.meta_app_secret,
                                                      "access_token": short["access_token"]})
        _raise_for(r)
        long = r.json()
    perms = short.get("permissions") or []
    if isinstance(perms, str):
        perms = perms.split(",")
    return {"access_token": long["access_token"], "user_id": str(short.get("user_id", "")),
            "expires_in": int(long.get("expires_in", 5184000)), "permissions": perms}


def refresh_token(token: str) -> dict:
    with _http() as http:
        r = http.get(f"{GRAPH}/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": token})
        _raise_for(r)
        return r.json()


# ------------------------------------------------------------------ client
class InstagramClient:
    def __init__(self, token: str, ig_user_id: str = "me"):
        self.token = token
        self.ig_user_id = ig_user_id or "me"
        self.base = f"{GRAPH}/{get_settings().ig_api_version}"
        self._http = _http()

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        try:
            r = self._http.get(f"{self.base}/{path}", params=params)
        except httpx.HTTPError as exc:
            raise InstagramError(f"Network error: {exc}", kind="transient") from exc
        _raise_for(r)
        return r.json()

    def _post(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        try:
            r = self._http.post(f"{self.base}/{path}", data=params)
        except httpx.HTTPError as exc:
            raise InstagramError(f"Network error: {exc}", kind="transient") from exc
        _raise_for(r)
        return r.json()

    # ---- profile & media
    def profile(self) -> dict:
        return self._get("me", fields="user_id,username,name,account_type,profile_picture_url,followers_count,"
                                      "follows_count,media_count,biography,website")

    def media(self, limit: int = 100, max_pages: int = 10) -> list[dict]:
        fields = ("id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url,"
                  "like_count,comments_count")
        out, after = [], None
        for _ in range(max_pages):
            params = {"fields": fields, "limit": min(limit, 100)}
            if after:
                params["after"] = after
            data = self._get(f"{self.ig_user_id}/media", **params)
            out += data.get("data", [])
            after = (data.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (data.get("paging") or {}).get("next") or len(out) >= limit:
                break
        return out[:limit]

    def media_item(self, media_id: str) -> dict:
        return self._get(media_id, fields="id,permalink,timestamp,media_type,media_product_type,caption")

    # ---- insights (per-metric fallback)
    def _insights(self, path: str, metrics: list[str], **params) -> tuple[dict[str, Any], dict[str, str]]:
        """Return ({metric: raw item}, {metric: unavailable reason})."""
        try:
            data = self._get(path, metric=",".join(metrics), **params).get("data", [])
            return {d["name"]: d for d in data}, {m: "Not returned by the API" for m in metrics
                                                  if m not in {d["name"] for d in data}}
        except InstagramError as exc:
            if exc.kind != "error":
                raise
            if len(metrics) == 1:
                return {}, {metrics[0]: str(exc)}
        # One unsupported metric fails the whole request, so ask for each metric separately.
        got, missing = {}, {}
        for m in metrics:
            try:
                for d in self._get(path, metric=m, **params).get("data", []):
                    got[d["name"]] = d
                if m not in got:
                    missing[m] = "Not returned by the API"
            except InstagramError as exc:
                if exc.kind != "error":
                    raise
                missing[m] = str(exc)
        return got, missing

    def account_totals(self, since: dt.date, until: dt.date) -> tuple[dict[str, float], dict[str, str]]:
        params = {"period": "day", "metric_type": "total_value",
                  "since": int(dt.datetime.combine(since, dt.time()).timestamp()),
                  "until": int(dt.datetime.combine(until, dt.time()).timestamp())}
        got, missing = self._insights(f"{self.ig_user_id}/insights", ACCOUNT_TOTAL_METRICS, **params)
        try:
            extra, _ = self._insights(f"{self.ig_user_id}/insights", ["follows_and_unfollows"],
                                      breakdown="follow_type", **params)
            got.update(extra)
            if not extra:
                missing["follows_and_unfollows"] = "Not returned by the API"
        except InstagramError as exc:
            if exc.kind != "error":
                raise
            missing["follows_and_unfollows"] = str(exc)
        values = {}
        for name, item in got.items():
            tv = item.get("total_value") or {}
            if "value" in tv:
                values[name] = float(tv["value"])
            for br in tv.get("breakdowns", []) or []:
                for res in br.get("results", []):
                    key = "_".join(str(x) for x in res.get("dimension_values", []))
                    values[f"{name}:{key}"] = float(res.get("value", 0))
        return values, missing

    def account_daily(self, since: dt.date, until: dt.date) -> tuple[dict[str, dict[dt.date, float]], dict[str, str]]:
        params = {"period": "day", "since": int(dt.datetime.combine(since, dt.time()).timestamp()),
                  "until": int(dt.datetime.combine(until, dt.time()).timestamp())}
        got, missing = self._insights(f"{self.ig_user_id}/insights", ACCOUNT_TIME_SERIES_METRICS, **params)
        series: dict[str, dict[dt.date, float]] = {}
        for name, item in got.items():
            for v in item.get("values", []):
                day = dt.datetime.fromisoformat(v["end_time"].replace("+0000", "+00:00")).date() - dt.timedelta(days=1)
                series.setdefault(name, {})[day] = float(v.get("value", 0))
        return series, missing

    def demographics(self, metric: str, breakdown: str, timeframe: str = "this_month") -> dict[str, float]:
        params = {"period": "lifetime", "metric_type": "total_value", "breakdown": breakdown}
        if metric == "engaged_audience_demographics" or timeframe:
            params["timeframe"] = timeframe
        data = self._get(f"{self.ig_user_id}/insights", metric=metric, **params).get("data", [])
        out: dict[str, float] = {}
        for item in data:
            for br in (item.get("total_value") or {}).get("breakdowns", []) or []:
                for res in br.get("results", []):
                    out["_".join(str(x) for x in res.get("dimension_values", []))] = float(res.get("value", 0))
        return out

    def media_insights(self, media_id: str, media_type: str, product_type: str) -> tuple[dict[str, float], dict[str, str]]:
        metrics = list(MEDIA_METRICS_COMMON)
        if product_type == "REELS":
            metrics += MEDIA_METRICS_REELS
        elif product_type != "STORY":
            metrics += MEDIA_METRICS_FEED
        got, missing = self._insights(f"{media_id}/insights", metrics)
        values = {}
        for name, item in got.items():
            vals = item.get("values") or []
            if vals:
                values[name] = float(vals[0].get("value", 0) or 0)
            elif "total_value" in item:
                values[name] = float(item["total_value"].get("value", 0))
        return values, missing

    # ---- publishing
    def publishing_limit(self) -> dict:
        data = self._get(f"{self.ig_user_id}/content_publishing_limit", fields="quota_usage,config").get("data", [{}])
        return data[0] if data else {}

    def create_image_container(self, image_url: str, caption: str | None, alt_text: str = "",
                               carousel_item: bool = False) -> str:
        params: dict[str, Any] = {"image_url": image_url}
        if carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption or ""
        if alt_text and get_settings().ig_send_alt_text:
            params["alt_text"] = alt_text[:1000]
        return self._post(f"{self.ig_user_id}/media", **params)["id"]

    def create_video_container(self, video_url: str, caption: str | None, *, reel: bool = True,
                               cover_url: str = "", carousel_item: bool = False) -> str:
        params: dict[str, Any] = {"video_url": video_url}
        if carousel_item:
            params.update(media_type="VIDEO", is_carousel_item="true")
        else:
            params.update(media_type="REELS", caption=caption or "", share_to_feed="true")
            if cover_url:
                params["cover_url"] = cover_url
        return self._post(f"{self.ig_user_id}/media", **params)["id"]

    def create_carousel_container(self, children: list[str], caption: str) -> str:
        return self._post(f"{self.ig_user_id}/media", media_type="CAROUSEL", children=",".join(children),
                          caption=caption)["id"]

    def container_status(self, container_id: str) -> dict:
        return self._get(container_id, fields="status_code,status")

    def wait_for_container(self, container_id: str, timeout: int = 600, interval: float = 5.0,
                           sleep: Callable[[float], None] = time.sleep) -> str:
        deadline = time.monotonic() + timeout
        while True:
            st = self.container_status(container_id)
            code = st.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return code
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Media container {code}: {st.get('status', '')}", kind="container")
            if time.monotonic() > deadline:
                raise InstagramError("Media container still processing after timeout", kind="transient")
            sleep(interval)

    def publish_container(self, container_id: str) -> str:
        return self._post(f"{self.ig_user_id}/media_publish", creation_id=container_id)["id"]

    # ---- comments
    def comments(self, media_id: str, limit: int = 50) -> list[dict]:
        return self._get(f"{media_id}/comments", fields="id,text,username,timestamp,like_count",
                         limit=limit).get("data", [])

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        return self._post(f"{comment_id}/replies", message=message)["id"]
