"""News discovery from RSS/Atom feeds, plus optional article-text excerpts for fact grounding.

Article text is stored only as a short private excerpt so the script writer can cite real
details; it is never republished verbatim (scripts must paraphrase and cite)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
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


BODY_CHARS = 4000


class _Paragraphs(HTMLParser):
    """Collects text inside <p> tags, skipping script/style/nav/footer."""

    SKIP = {"script", "style", "nav", "footer", "aside", "header", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._in_p = 0
        self._skip = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p":
            self._in_p += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            text = " ".join("".join(self._buf).split())
            if len(text) > 40:
                self.parts.append(text)
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_p and not self._skip:
            self._buf.append(data)


def extract_text(html: str, limit: int = BODY_CHARS) -> str:
    parser = _Paragraphs()
    parser.feed(html)
    return "\n".join(parser.parts)[:limit]


def fetch_bodies(
    conn: sqlite3.Connection, limit: int = 20, client: httpx.Client | None = None
) -> dict[str, int]:
    """Fetch article text for official/reputable items that have none yet."""
    rows = conn.execute(
        "SELECT id, url FROM items WHERE body = '' AND tier IN ('official', 'reputable')"
        " ORDER BY CASE tier WHEN 'official' THEN 0 ELSE 1 END, published_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    own = client is None
    http = client or httpx.Client(timeout=20)
    report = {"tried": 0, "filled": 0}
    try:
        for r in rows:
            report["tried"] += 1
            try:
                resp = http.get(r["url"], headers={"User-Agent": USER_AGENT}, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            text = extract_text(resp.text)
            if text:
                conn.execute("UPDATE items SET body = ? WHERE id = ?", (text, r["id"]))
                report["filled"] += 1
    finally:
        if own:
            http.close()
    return report


def research(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[sqlite3.Row]:
    """Items with article text matching every query word, best tier first."""
    words = [w.lower() for w in query.split() if len(w) > 2]
    rows = conn.execute(
        "SELECT title, url, domain, tier, published_at, body FROM items WHERE body != ''"
        " AND tier IN ('official', 'reputable')"
        " ORDER BY CASE tier WHEN 'official' THEN 0 ELSE 1 END, published_at DESC"
    ).fetchall()
    hits = [r for r in rows if all(w in f"{r['title']} {r['body']}".lower() for w in words)]
    return hits[:limit]
