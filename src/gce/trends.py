"""Trend intake from viral channels via the official YouTube Data API v3.

We read only public metadata (titles, publish time, views, likes, comments, duration) to
learn which TOPICS and FORMATS are working. We never download, re-upload or quote their
video, audio, thumbnails or scripts. Every idea taken from a trend is logged in the
`inspirations` table with its source URL.

API (https://developers.google.com/youtube/v3), key in YOUTUBE_API_KEY, quota 10,000
units/day by default. Calls used, all 1 unit each:
  channels.list?forHandle=@x&part=snippet,contentDetails,statistics
  playlistItems.list?playlistId=<uploads>&part=contentDetails&maxResults<=50
  videos.list?id=a,b,c&part=snippet,statistics,contentDetails   (<=50 ids)
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx

from gce import db
from gce.config import secret

API = "https://www.googleapis.com/youtube/v3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trend_videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    duration_s INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);
"""

# Hook/format patterns seen in viral gaming Shorts (EN + ES). Used as signals only.
PATTERNS: dict[str, re.Pattern[str]] = {
    "numbered_list": re.compile(r"(^|\s)(top\s*)?\d{1,2}\s+\w+", re.I),
    "forbidden_secret": re.compile(
        r"doesn'?t want you|secret|hidden|nobody (knows|noticed)|you (missed|didn'?t know)|"
        r"secreto|oculto|nadie sabe|no sab[ií]as",
        re.I,
    ),
    "reason_reveal": re.compile(r"(this is|here'?s) (the reason|why)|la raz[oó]n|por qu[eé]", re.I),
    "compilation": re.compile(
        r"situations|moments|times when|players who|situaciones|momentos|jugadores que", re.I
    ),
    "superlative": re.compile(
        r"\b(best|worst|most|rarest|fastest|brutal|insane|mejor|peor|"
        r"m[aá]s r[aá]pido|brutal)\b",
        re.I,
    ),
    "question": re.compile(r"\?"),
}
STOP = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "and",
    "you",
    "your",
    "is",
    "it",
    "this",
    "that",
    "for",
    "with",
    "gta",
    "gta5",
    "gta6",
    "gtav",
    "gtavi",
    "5",
    "6",
    "v",
    "vi",
    "el",
    "la",
    "los",
    "las",
    "de",
    "en",
    "que",
    "y",
    "un",
    "una",
    "se",
    "lo",
    "por",
    "para",
    "con",
    "del",
    "doesn't",
    "want",
    "find",
    "found",
    "grand",
    "theft",
    "auto",
    "online",
    "shorts",
    "short",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def iso_duration_s(value: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def patterns_of(title: str) -> list[str]:
    return [name for name, rx in PATTERNS.items() if rx.search(title)]


def keywords(title: str) -> list[str]:
    clean = re.sub(r"#\w+", " ", title.lower())
    words = re.findall(r"[a-záéíóúñü0-9']+", clean)
    return [w for w in words if w not in STOP and len(w) > 2 and not w.isdigit()]


class Client:
    def __init__(self, http: httpx.Client | None = None) -> None:
        key = secret("YOUTUBE_API_KEY")
        if not key:
            raise RuntimeError(
                "YOUTUBE_API_KEY is not set (Google Cloud console -> enable YouTube Data API v3 "
                "-> Credentials -> API key)"
            )
        self.key = key
        self.http = http or httpx.Client(timeout=30)

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self.http.get(f"{API}/{path}", params={**params, "key": self.key})
        if resp.status_code >= 400:
            raise RuntimeError(f"YouTube API {path} -> {resp.status_code}: {resp.text[:300]}")
        data: dict[str, Any] = resp.json()
        return data

    def channel(self, handle: str) -> dict[str, Any]:
        data = self._get(
            "channels",
            part="snippet,contentDetails,statistics",
            forHandle=handle if handle.startswith("@") else f"@{handle}",
        )
        items = data.get("items") or []
        if not items:
            raise RuntimeError(f"channel not found: {handle}")
        item: dict[str, Any] = items[0]
        return item

    def recent_video_ids(self, uploads_playlist: str, limit: int) -> list[str]:
        data = self._get(
            "playlistItems",
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=min(limit, 50),
        )
        return [i["contentDetails"]["videoId"] for i in data.get("items", [])]

    def videos(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        data = self._get("videos", part="snippet,statistics,contentDetails", id=",".join(ids))
        items: list[dict[str, Any]] = data.get("items", [])
        return items


def pull(
    conn: sqlite3.Connection,
    handles: list[str],
    per_channel: int = 15,
    client: Client | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    yt = client or Client()
    report: dict[str, Any] = {}
    for handle in handles:
        try:
            ch = yt.channel(handle)
            uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
            vids = yt.videos(yt.recent_video_ids(uploads, per_channel))
        except (RuntimeError, KeyError, httpx.HTTPError) as exc:
            report[handle] = f"error: {exc}"
            continue
        for v in vids:
            st = v.get("statistics", {})
            sn = v["snippet"]
            conn.execute(
                "INSERT INTO trend_videos (video_id, channel_id, channel_title, title,"
                " published_at, duration_s, views, likes, comments, captured_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(video_id) DO UPDATE SET"
                " views=excluded.views, likes=excluded.likes, comments=excluded.comments,"
                " title=excluded.title, captured_at=excluded.captured_at",
                (
                    v["id"],
                    sn["channelId"],
                    sn["channelTitle"],
                    sn["title"],
                    sn["publishedAt"],
                    iso_duration_s(v.get("contentDetails", {}).get("duration", "")),
                    int(st.get("viewCount", 0)),
                    int(st.get("likeCount", 0)),
                    int(st.get("commentCount", 0)),
                    db.now(),
                ),
            )
        report[handle] = f"{len(vids)} videos"
    return report


def ranked(
    conn: sqlite3.Connection, days: int = 14, shorts_only: bool = True, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Videos ranked by outlier score: views/hour relative to their channel's median."""
    ensure_schema(conn)
    now = now or datetime.now(UTC)
    rows = conn.execute("SELECT * FROM trend_videos").fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        published = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
        age_h = max(1.0, (now - published).total_seconds() / 3600)
        if age_h > days * 24 or (shorts_only and not 0 < r["duration_s"] <= 180):
            continue
        out.append({**dict(r), "age_h": round(age_h, 1), "vph": r["views"] / age_h})
    by_channel: dict[str, list[float]] = {}
    for v in out:
        by_channel.setdefault(v["channel_id"], []).append(v["vph"])
    for v in out:
        median = statistics.median(by_channel[v["channel_id"]]) or 1.0
        v["outlier"] = round(v["vph"] / median, 2)
        v["vph"] = round(v["vph"], 1)
        v["patterns"] = patterns_of(v["title"])
        v["url"] = f"https://www.youtube.com/shorts/{v['video_id']}"
    return sorted(out, key=lambda v: (v["outlier"], v["vph"]), reverse=True)


def signals(videos: list[dict[str, Any]], top: int = 15) -> dict[str, Any]:
    """Which formats and topics over-perform, weighted by outlier score."""
    pat: Counter[str] = Counter()
    kw: Counter[str] = Counter()
    for v in videos:
        weight = v["outlier"]
        for p in v["patterns"]:
            pat[p] += weight
        for k in set(keywords(v["title"])):
            kw[k] += weight
    return {
        "patterns": [(p, round(s, 2)) for p, s in pat.most_common()],
        "topics": [(k, round(s, 2)) for k, s in kw.most_common(top)],
    }


def log_inspiration(conn: sqlite3.Connection, script_id: int, video: sqlite3.Row) -> None:
    conn.execute(
        "INSERT INTO inspirations (script_id, url, creator, platform, note, created_at)"
        " VALUES (?, ?, ?, 'youtube', ?, ?)",
        (
            script_id,
            f"https://www.youtube.com/shorts/{video['video_id']}",
            video["channel_title"],
            f"topic/format signal only: {video['title'][:120]}",
            db.now(),
        ),
    )
