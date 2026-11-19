"""News discovery from RSS/Atom feeds. Collects headlines and links only; no full-text scraping."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import struct_time
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

from gce import db
from gce.config import DiscoveryCfg, VerificationCfg

USER_AGENT = "gta6-content-engine/0.1 (+rss reader)"


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    summary: str
    domain: str
    feed: str
    published_at: datetime | None


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def classify_tier(url: str, cfg: VerificationCfg) -> str:
    """official | reputable | other, matching on host suffix or host+path prefix."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    host_path = f"{host}{parsed.path}".lower()

    def matches(entry: str) -> bool:
        if "/" in entry:
            return host_path.startswith(entry)
        return host == entry or host.endswith("." + entry)

    if any(matches(e) for e in cfg.official_domains):
        return "official"
    if any(matches(e) for e in cfg.reputable_domains):
        return "reputable"
    return "other"


def _published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if isinstance(value, struct_time):
            return datetime(*value[:6], tzinfo=UTC)
    return None


def parse_feed(content: bytes | str, feed_url: str) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        link = str(entry.get("link", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not link or not title:
            continue
        # Aggregators (e.g. Google News) wrap links; the real publisher is in <source url=...>.
        source = entry.get("source") or {}
        source_href = source.get("href") if isinstance(source, dict) else None
        domain = host_of(str(source_href)) if source_href else host_of(link)
        items.append(
            FeedItem(
                url=link,
                title=title,
                summary=str(entry.get("summary", ""))[:1000],
                domain=domain,
                feed=feed_url,
                published_at=_published(entry),
            )
        )
    return items


def is_relevant(item: FeedItem, keywords: list[str]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    return any(k in text for k in keywords)


def fetch(feed_url: str, client: httpx.Client) -> list[FeedItem]:
    resp = client.get(feed_url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    resp.raise_for_status()
    return parse_feed(resp.content, feed_url)


def store(
    conn: sqlite3.Connection,
    items: list[FeedItem],
    dcfg: DiscoveryCfg,
    vcfg: VerificationCfg,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=dcfg.max_age_days)
    added = 0
    for item in items:
        if not is_relevant(item, dcfg.keywords):
            continue
        if item.published_at and item.published_at < cutoff:
            continue
        tier = classify_tier(f"https://{item.domain}/", vcfg)
        if tier == "other":  # host+path entries (e.g. x.com/RockstarGames) need the full URL
            tier = classify_tier(item.url, vcfg)
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (url, title, summary, domain, feed, published_at,"
            " fetched_at, tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.url,
                item.title,
                item.summary,
                item.domain,
                item.feed,
                item.published_at.isoformat() if item.published_at else None,
                db.now(),
                tier,
            ),
        )
        added += cur.rowcount
    return added


def run(conn: sqlite3.Connection, dcfg: DiscoveryCfg, vcfg: VerificationCfg) -> dict[str, Any]:
    report: dict[str, Any] = {"feeds": {}, "added": 0}
    with httpx.Client(timeout=20) as client:
        for feed_url in dcfg.feeds:
            try:
                items = fetch(feed_url, client)
            except (httpx.HTTPError, ValueError) as exc:
                report["feeds"][feed_url] = f"error: {exc}"
                continue
            added = store(conn, items, dcfg, vcfg)
            report["feeds"][feed_url] = f"{len(items)} items, {added} new relevant"
            report["added"] += added
    return report
