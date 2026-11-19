"""Zernio adapter (https://docs.zernio.com), verified against the public docs:

- Base URL: https://zernio.com/api/v1 ; header Authorization: Bearer {ZERNIO_API_KEY}
- GET  /accounts                      -> connected social accounts (ids are 24-char `_id`)
- POST /media/presign {filename, contentType, size} -> {uploadUrl, publicUrl, key, expiresIn}
  then PUT the file to uploadUrl with the same Content-Type and no Authorization header
- POST /posts {content, platforms:[{platform, accountId}], mediaItems:[{url, type}],
               publishNow | scheduledFor + timezone, tiktokSettings{...}}
  TikTok requires tiktokSettings.content_preview_confirmed and express_consent_given = true,
  and privacy_level must be one the creator allows.
- Per-platform options go in platforms[].platformSpecificData (verified 2026-11-19):
  youtube:   title (<=100), visibility, madeForKids, containsSyntheticMedia, categoryId
             ("20" Gaming); tags are top-level. Vertical video <=3 min is a Short.
  facebook:  contentType "reel" (9:16, 3-60 s; 30 API Reels per Page per 24 h)
  instagram: a single video always publishes as a Reel; shareToFeed (default true)
- GET  /posts/{id}                    -> post.status: scheduled | publishing | published
- GET  /analytics?postId=...          -> analytics{impressions, reach, likes, comments, shares,
                                         saves, clicks, views, engagementRate}
- 429 => honor Retry-After.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from gce.config import ZernioCfg, secret


class ZernioError(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: ZernioCfg, http: httpx.Client | None = None) -> None:
        key = secret("ZERNIO_API_KEY")
        if not key:
            raise ZernioError("ZERNIO_API_KEY is not set")
        self.cfg = cfg
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {key}"}

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        for attempt in range(4):
            resp = self.http.request(
                method, f"{self.cfg.base_url}{path}", headers=self.headers, **kw
            )
            if resp.status_code == 429 and attempt < 3:
                time.sleep(float(resp.headers.get("Retry-After", "2")))
                continue
            if resp.status_code >= 400:
                raise ZernioError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            data: dict[str, Any] = resp.json() if resp.content else {}
            return data
        raise ZernioError(f"{method} {path}: rate limited")

    def accounts(self) -> dict[str, Any]:
        return self._req("GET", "/accounts")

    def upload(self, path: Path, content_type: str = "video/mp4") -> str:
        size = path.stat().st_size
        presign = self._req(
            "POST",
            "/media/presign",
            json={"filename": path.name, "contentType": content_type, "size": size},
        )
        with path.open("rb") as fh:
            put = self.http.put(
                presign["uploadUrl"], content=fh.read(), headers={"Content-Type": content_type}
            )
        if put.status_code >= 400:
            raise ZernioError(f"upload PUT failed {put.status_code}: {put.text[:300]}")
        return str(presign["publicUrl"])

    def create_post(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/posts", json=body)

    def get_post(self, post_id: str) -> dict[str, Any]:
        return self._req("GET", f"/posts/{post_id}")

    def analytics(self, post_id: str) -> dict[str, Any]:
        return self._req("GET", "/analytics", params={"postId": post_id})


def build_post_body(
    *,
    caption: str,
    media_url: str,
    platforms: list[str],
    accounts: dict[str, str],
    timezone: str,
    scheduled_for: str | None,
    tiktok_privacy: str,
    title: str = "",
    tags: list[str] | None = None,
    ai_generated: bool = False,
) -> dict[str, Any]:
    missing = [p for p in platforms if not accounts.get(p)]
    if missing:
        raise ZernioError(f"no Zernio accountId configured for: {', '.join(missing)}")
    body: dict[str, Any] = {
        "content": caption,
        "platforms": [
            {
                "platform": p,
                "accountId": accounts[p],
                **(
                    {"platformSpecificData": psd}
                    if (psd := _platform_data(p, title, ai_generated))
                    else {}
                ),
            }
            for p in platforms
        ],
        "mediaItems": [{"url": media_url, "type": "video"}],
    }
    if tags and "youtube" in platforms:
        body["tags"] = _youtube_tags(tags)
    if scheduled_for:
        body["scheduledFor"] = scheduled_for
        body["timezone"] = timezone
    else:
        body["publishNow"] = True
    if "tiktok" in platforms:
        body["tiktokSettings"] = {
            "privacy_level": tiktok_privacy,
            "allow_comment": True,
            "allow_duet": False,
            "allow_stitch": False,
            "content_preview_confirmed": True,
            "express_consent_given": True,
        }
    return body


def _platform_data(platform: str, title: str, ai_generated: bool) -> dict[str, Any]:
    if platform == "youtube":
        return {
            "title": (title or "Untitled")[:100],
            "visibility": "public",
            "madeForKids": False,  # M-rated game, adult audience; never kid-directed
            "containsSyntheticMedia": ai_generated,
            "categoryId": "20",  # Gaming
        }
    if platform == "facebook":
        return {"contentType": "reel", **({"title": title[:255]} if title else {})}
    if platform == "instagram":
        return {"shareToFeed": True}
    return {}


def _youtube_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    total = 0
    for t in tags:
        t = t.lstrip("#")[:100]
        if total + len(t) > 500:
            break
        out.append(t)
        total += len(t)
    return out


def extract_metrics(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Find the first `analytics` object in a /analytics response."""
    candidates: list[Any] = []
    for key in ("posts", "data", "items"):
        if isinstance(payload.get(key), list):
            candidates += payload[key]
    candidates.append(payload)
    for c in candidates:
        if isinstance(c, dict) and isinstance(c.get("analytics"), dict):
            result: dict[str, Any] = c["analytics"]
            return result
    return None
