"""Fact verification: cluster headlines into stories and grade each by source quality.

Rules (deliberately conservative):
- CONFIRMED: at least one official (tier-1) source, e.g. rockstargames.com.
- CORROBORATED: two or more independent reputable outlets, no official source.
  Scripts must attribute ("IGN and Bloomberg report...").
- UNVERIFIED: everything else. Only usable as clearly labeled speculation, never as news.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "to",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "be",
    "gta",
    "6",
    "vi",
    "grand",
    "theft",
    "auto",
    "rockstar",
    "games",
    "new",
    "just",
    "about",
    "it",
    "its",
    "this",
    "that",
    "has",
    "have",
    "will",
    "from",
    "at",
    "as",
    "by",
    "you",
    "your",
}
HOT_TERMS = {
    "trailer": 3.0,
    "release": 2.5,
    "date": 1.5,
    "delay": 3.0,
    "delayed": 3.0,
    "price": 2.0,
    "preorder": 2.5,
    "pre-order": 2.5,
    "gameplay": 2.0,
    "map": 1.5,
    "screenshots": 1.5,
    "official": 1.0,
    "announced": 1.5,
    "confirmed": 1.0,
    "earnings": 1.5,
    "pc": 1.0,
}
SIMILARITY_THRESHOLD = 0.45


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9'-]+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Cluster:
    title: str
    toks: set[str]
    item_ids: list[int] = field(default_factory=list)
    tiers: dict[str, set[str]] = field(default_factory=dict)  # tier -> domains
    first_seen: str = ""
    last_seen: str = ""


def status_for(official: int, reputable: int) -> str:
    if official >= 1:
        return "confirmed"
    if reputable >= 2:
        return "corroborated"
    return "unverified"


def score(cluster: Cluster, status: str, now: datetime) -> float:
    base = {"confirmed": 10.0, "corroborated": 6.0, "unverified": 1.0}[status]
    breadth = sum(len(d) for d in cluster.tiers.values())
    heat = sum(w for t, w in HOT_TERMS.items() if t in cluster.toks)
    recency = 0.0
    if cluster.last_seen:
        age_h = (now - datetime.fromisoformat(cluster.last_seen)).total_seconds() / 3600
        recency = max(0.0, 5.0 - age_h / 24)  # decays to 0 over ~5 days
    return round(base + min(breadth, 8) * 0.75 + heat + recency, 2)


def cluster_items(rows: list[sqlite3.Row]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for row in rows:
        toks = tokens(row["title"])
        best, best_sim = None, 0.0
        for c in clusters:
            sim = jaccard(toks, c.toks)
            if sim > best_sim:
                best, best_sim = c, sim
        when = row["published_at"] or row["fetched_at"]
        if best is None or best_sim < SIMILARITY_THRESHOLD:
            best = Cluster(title=row["title"], toks=set(toks), first_seen=when, last_seen=when)
            clusters.append(best)
        else:
            best.toks |= toks
            best.first_seen = min(best.first_seen, when)
            best.last_seen = max(best.last_seen, when)
        best.item_ids.append(row["id"])
        best.tiers.setdefault(row["tier"], set()).add(row["domain"])
        # Prefer an official headline as the story title.
        if row["tier"] == "official":
            best.title = row["title"]
    return clusters


def run(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT * FROM items WHERE story_id IS NULL ORDER BY COALESCE(published_at, fetched_at)"
    ).fetchall()
    now = datetime.now(UTC)
    out: list[dict[str, object]] = []
    for c in cluster_items(rows):
        official = len(c.tiers.get("official", set()))
        reputable = len(c.tiers.get("reputable", set()))
        other = len(c.tiers.get("other", set()))
        status = status_for(official, reputable)
        s = score(c, status, now)
        cur = conn.execute(
            "INSERT INTO stories (title, status, score, official_count, reputable_count,"
            " other_count, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (c.title, status, s, official, reputable, other, c.first_seen, c.last_seen),
        )
        conn.executemany(
            "UPDATE items SET story_id = ? WHERE id = ?", [(cur.lastrowid, i) for i in c.item_ids]
        )
        out.append({"story_id": cur.lastrowid, "title": c.title, "status": status, "score": s})
    return out


def sources_for(conn: sqlite3.Connection, story_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT title, url, domain, tier, published_at FROM items WHERE story_id = ?"
        " ORDER BY CASE tier WHEN 'official' THEN 0 WHEN 'reputable' THEN 1 ELSE 2 END",
        (story_id,),
    ).fetchall()


def top_stories(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stories WHERE used = 0 ORDER BY score DESC LIMIT ?", (limit,)
    ).fetchall()
