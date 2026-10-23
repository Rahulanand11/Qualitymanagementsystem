"""In-memory stand-in for graph.instagram.com used by the tests (no network)."""
from __future__ import annotations

import itertools
import json
from urllib.parse import parse_qs

import httpx


class FakeGraph:
    def __init__(self):
        self.ids = itertools.count(1000)
        self.containers: dict[str, dict] = {}
        self.media: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_next_publish: dict | None = None
        self.container_progress_polls = 0
        self.quota_usage = 0
        self.unsupported_metrics = {"replies", "profile_links_taps"}
        self.demographics_available = {"country", "age"}
        self.comments: dict[str, list[dict]] = {}
        self.replies: list[tuple[str, str]] = []
        self.fetched_urls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _err(code: int, msg: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": msg, "code": code, "type": "OAuthException"}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.method == "POST" and request.content:
            params.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        path = request.url.path
        self.calls.append((request.method, path, params))
        host = request.url.host
        if host == "api.instagram.com" and path == "/oauth/access_token":
            return httpx.Response(200, json={"access_token": "SHORT_TOKEN", "user_id": 17841400000000001,
                                             "permissions": "instagram_business_basic,instagram_business_content_publish"})
        if path == "/access_token":
            return httpx.Response(200, json={"access_token": "LONG_TOKEN_SECRET_123", "token_type": "bearer",
                                             "expires_in": 5183944})
        if path == "/refresh_access_token":
            return httpx.Response(200, json={"access_token": "REFRESHED_TOKEN", "expires_in": 5183944})
        parts = [p for p in path.split("/") if p][1:]  # drop version
        if not params.get("access_token"):
            return self._err(190, "Missing token", 401)

        if parts == ["me"]:
            return httpx.Response(200, json={"user_id": "17841400000000001", "username": "creator_demo",
                                             "name": "Creator Demo", "account_type": "MEDIA_CREATOR",
                                             "followers_count": 1520, "follows_count": 300, "media_count": len(self.media),
                                             "biography": "Travel photographer sharing city guides", "website": "",
                                             "profile_picture_url": "https://example.com/p.jpg"})
        if len(parts) == 2 and parts[1] == "content_publishing_limit":
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota_usage,
                                                       "config": {"quota_total": 50, "quota_duration": 86400}}]})
        if len(parts) == 2 and parts[1] == "media" and request.method == "POST":
            cid = str(next(self.ids))
            url = params.get("image_url") or params.get("video_url")
            self.fetched_urls.append(url)
            self.containers[cid] = {"params": params, "status": "IN_PROGRESS" if params.get("media_type") == "REELS" else "FINISHED"}
            return httpx.Response(200, json={"id": cid})
        if len(parts) == 2 and parts[1] == "media":
            return httpx.Response(200, json={"data": list(reversed(self.media))})
        if len(parts) == 2 and parts[1] == "media_publish":
            if self.fail_next_publish:
                err, self.fail_next_publish = self.fail_next_publish, None
                return self._err(err["code"], err["message"], err.get("status", 400))
            c = self.containers[params["creation_id"]]
            c["status"] = "PUBLISHED"
            mid = str(next(self.ids))
            mtype = "CAROUSEL_ALBUM" if c["params"].get("media_type") == "CAROUSEL" else (
                "VIDEO" if c["params"].get("media_type") == "REELS" else "IMAGE")
            self.media.append({"id": mid, "caption": c["params"].get("caption", ""), "media_type": mtype,
                               "media_product_type": "REELS" if mtype == "VIDEO" else "FEED",
                               "permalink": f"https://www.instagram.com/p/{mid}/",
                               "timestamp": "2026-10-20T12:00:00+0000", "like_count": 10, "comments_count": 1})
            return httpx.Response(200, json={"id": mid})
        if len(parts) == 2 and parts[1] == "insights":
            return self._insights(parts[0], params)
        if len(parts) == 2 and parts[1] == "comments":
            return httpx.Response(200, json={"data": self.comments.get(parts[0], [])})
        if len(parts) == 2 and parts[1] == "replies":
            self.replies.append((parts[0], params["message"]))
            return httpx.Response(200, json={"id": str(next(self.ids))})
        if len(parts) == 1 and parts[0] in self.containers:
            c = self.containers[parts[0]]
            if c["status"] == "IN_PROGRESS":
                self.container_progress_polls += 1
                if self.container_progress_polls >= 2:
                    c["status"] = "FINISHED"
                return httpx.Response(200, json={"status_code": "IN_PROGRESS", "status": "Processing"})
            return httpx.Response(200, json={"status_code": c["status"]})
        if len(parts) == 1:
            m = next((m for m in self.media if m["id"] == parts[0]), None)
            if m:
                return httpx.Response(200, json=m)
        return self._err(100, f"Unknown path {path}")

    def _insights(self, obj: str, params: dict) -> httpx.Response:
        metrics = params["metric"].split(",")
        if any(m in self.unsupported_metrics for m in metrics):
            return self._err(100, "(#100) metric[0] must be one of the following values: ...")
        if obj == "17841400000000001" or obj == "me":
            if metrics[0] in ("follower_demographics", "engaged_audience_demographics"):
                bd = params.get("breakdown")
                if bd not in self.demographics_available:
                    return self._err(100, "Not enough data for this breakdown")
                data = {"country": [("US", 600), ("IN", 300), ("GB", 120)], "age": [("18-24", 400), ("25-34", 700)]}[bd]
                return httpx.Response(200, json={"data": [{"name": metrics[0], "total_value": {"breakdowns": [
                    {"dimension_keys": [bd], "results": [{"dimension_values": [k], "value": v} for k, v in data]}]}}]})
            if params.get("metric_type") == "total_value":
                out = []
                for m in metrics:
                    item = {"name": m, "period": "day", "total_value": {"value": {"reach": 800, "views": 2400}.get(m, 50)}}
                    if m == "follows_and_unfollows" and params.get("breakdown") == "follow_type":
                        item["total_value"] = {"breakdowns": [{"dimension_keys": ["follow_type"], "results": [
                            {"dimension_values": ["FOLLOWER"], "value": 12}, {"dimension_values": ["NON_FOLLOWER"], "value": 3}]}]}
                    out.append(item)
                return httpx.Response(200, json={"data": out})
            return httpx.Response(200, json={"data": [
                {"name": m, "period": "day", "values": [{"value": 20, "end_time": "2026-10-19T07:00:00+0000"},
                                                        {"value": 25, "end_time": "2026-10-20T07:00:00+0000"}]}
                for m in metrics]})
        vals = {"reach": 900, "views": 3000, "likes": 70, "comments": 5, "shares": 12, "saved": 20,
                "total_interactions": 107, "profile_visits": 15, "follows": 4,
                "ig_reels_avg_watch_time": 5200, "ig_reels_video_view_total_time": 900000}
        return httpx.Response(200, json={"data": [{"name": m, "period": "lifetime", "values": [{"value": vals.get(m, 0)}]}
                                                  for m in metrics]})


def body_json(resp: httpx.Response) -> dict:
    return json.loads(resp.content)
