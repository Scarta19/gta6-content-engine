from __future__ import annotations

import sqlite3

from gce import config, discovery, verify

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>GTA 6 trailer 3 released by Rockstar</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <pubDate>Mon, 16 Nov 2026 15:00:00 GMT</pubDate>
  <source url="https://www.ign.com">IGN</source></item>
<item><title>GTA 6 trailer 3 released, watch here</title>
  <link>https://www.polygon.com/gta-6-trailer-3</link>
  <pubDate>Mon, 16 Nov 2026 16:00:00 GMT</pubDate></item>
<item><title>Leaker claims GTA 6 map is huge</title>
  <link>https://www.reddit.com/r/GTA6/comments/x</link>
  <pubDate>Mon, 16 Nov 2026 17:00:00 GMT</pubDate></item>
<item><title>Best pizza in Chicago</title>
  <link>https://example.com/pizza</link></item>
</channel></rss>"""


def test_classify_tier(cfg: config.Config) -> None:
    v = cfg.verification
    assert discovery.classify_tier("https://www.rockstargames.com/newswire/1", v) == "official"
    assert discovery.classify_tier("https://x.com/RockstarGames/status/1", v) == "official"
    assert discovery.classify_tier("https://x.com/someleaker/status/1", v) == "other"
    assert discovery.classify_tier("https://uk.ign.com/a", v) == "reputable"
    assert discovery.classify_tier("https://notign.com/a", v) == "other"


def test_parse_uses_publisher_domain() -> None:
    items = discovery.parse_feed(RSS, "feed")
    assert items[0].domain == "ign.com"
    assert items[1].domain == "polygon.com"


def test_store_filters_and_dedupes(cfg: config.Config, conn: sqlite3.Connection) -> None:
    items = discovery.parse_feed(RSS, "feed")
    dcfg = config.DiscoveryCfg(feeds=[], keywords=cfg.discovery.keywords, max_age_days=10_000)
    assert discovery.store(conn, items, dcfg, cfg.verification) == 3  # pizza dropped
    assert discovery.store(conn, items, dcfg, cfg.verification) == 0  # idempotent


def test_verify_clusters_and_grades(cfg: config.Config, conn: sqlite3.Connection) -> None:
    dcfg = config.DiscoveryCfg(feeds=[], keywords=cfg.discovery.keywords, max_age_days=10_000)
    discovery.store(conn, discovery.parse_feed(RSS, "feed"), dcfg, cfg.verification)
    stories = {s["title"]: s for s in verify.run(conn)}
    trailer = next(s for t, s in stories.items() if "trailer" in t)
    leak = next(s for t, s in stories.items() if "Leaker" in t)
    assert trailer["status"] == "corroborated"  # IGN + Polygon, no official source
    assert leak["status"] == "unverified"
    assert trailer["score"] > leak["score"]  # type: ignore[operator]
    assert verify.run(conn) == []  # items already assigned


def test_status_rules() -> None:
    assert verify.status_for(1, 0) == "confirmed"
    assert verify.status_for(0, 2) == "corroborated"
    assert verify.status_for(0, 1) == "unverified"
