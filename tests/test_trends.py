from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from gce import trends


def test_iso_duration() -> None:
    assert trends.iso_duration_s("PT58S") == 58
    assert trends.iso_duration_s("PT1M5S") == 65
    assert trends.iso_duration_s("PT1H") == 3600
    assert trends.iso_duration_s("garbage") == 0


def test_patterns_en_es() -> None:
    assert set(trends.patterns_of("Found 3 Brutal Cars GTA 5 Doesn't Want You to Find!")) >= {
        "numbered_list",
        "forbidden_secret",
        "superlative",
    }
    assert "reason_reveal" in trends.patterns_of("This is the reason GTA6 Delayed Again")
    assert "compilation" in trends.patterns_of("Jugadores que parecen tener prohibido chocarse")


def _video(vid: str, ch: str, title: str, views: int, published: str, dur: str = "PT40S") -> dict:
    return {
        "id": vid,
        "snippet": {
            "channelId": ch,
            "channelTitle": ch.upper(),
            "title": title,
            "publishedAt": published,
        },
        "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
        "contentDetails": {"duration": dur},
    }


def test_pull_rank_and_signals(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    vids = [
        _video("a", "c1", "Found 3 secret cars nobody knows", 900_000, "2026-11-18T00:00:00Z"),
        _video("b", "c1", "Normal upload", 30_000, "2026-11-18T00:00:00Z"),
        _video("c", "c1", "Another normal one", 40_000, "2026-11-18T00:00:00Z"),
        _video("d", "c1", "Long video", 5_000_000, "2026-11-18T00:00:00Z", "PT12M"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["key"] == "k"
        if req.url.path.endswith("/channels"):
            assert req.url.params["forHandle"] == "@chan"
            return httpx.Response(
                200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
            )
        if req.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200, json={"items": [{"contentDetails": {"videoId": v["id"]}} for v in vids]}
            )
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": vids})
        return httpx.Response(404)

    client = trends.Client(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert trends.pull(conn, ["chan"], client=client) == {"chan": "4 videos"}
    ranked = trends.ranked(conn, days=14, now=datetime(2026, 11, 19, tzinfo=UTC))
    assert [r["video_id"] for r in ranked][0] == "a"
    assert "d" not in {r["video_id"] for r in ranked}  # long-form excluded
    assert ranked[0]["outlier"] > 1
    sig = trends.signals(ranked)
    assert sig["patterns"][0][0] in {"numbered_list", "forbidden_secret"}
    assert any(k == "cars" for k, _ in sig["topics"])


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YOUTUBE_API_KEY"):
        trends.Client()
