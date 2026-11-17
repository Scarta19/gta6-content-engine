"""SQLite storage: content, provenance, approvals, publications, metrics and costs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL,
    feed TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'unknown',          -- official | reputable | other
    story_id INTEGER REFERENCES stories(id)
);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,                          -- confirmed | corroborated | unverified
    score REAL NOT NULL DEFAULT 0,
    official_count INTEGER NOT NULL DEFAULT 0,
    reputable_count INTEGER NOT NULL DEFAULT 0,
    other_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY,
    story_id INTEGER REFERENCES stories(id),
    format TEXT NOT NULL,                          -- news | analysis | concept
    title TEXT NOT NULL,
    hook TEXT NOT NULL,
    voiceover TEXT NOT NULL,
    on_screen_json TEXT NOT NULL DEFAULT '[]',
    cta TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags TEXT NOT NULL DEFAULT '',
    visual_plan TEXT NOT NULL DEFAULT '',
    claims_json TEXT NOT NULL DEFAULT '[]',
    rights_notes TEXT NOT NULL DEFAULT '',
    ai_generated INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,                            -- image | video | audio
    license TEXT NOT NULL,                         -- see assets.LICENSES
    source TEXT NOT NULL,
    provenance_url TEXT NOT NULL DEFAULT '',
    ai_generated INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS script_assets (
    script_id INTEGER NOT NULL REFERENCES scripts(id),
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (script_id, asset_id)
);
CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY,
    script_id INTEGER NOT NULL REFERENCES scripts(id),
    path TEXT NOT NULL,
    srt_path TEXT NOT NULL,
    duration_s REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | approved | rejected
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    render_id INTEGER NOT NULL REFERENCES renders(id),
    decision TEXT NOT NULL,                        -- approved | rejected
    reviewer TEXT NOT NULL,
    checklist_json TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY,
    render_id INTEGER NOT NULL REFERENCES renders(id),
    platform TEXT NOT NULL,
    mode TEXT NOT NULL,                            -- dry_run | live | manual
    external_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    captured_at TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0,
    reach INTEGER NOT NULL DEFAULT 0,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    shares INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    engagement_rate REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gate_runs (
    id INTEGER PRIMARY KEY,
    render_id INTEGER NOT NULL REFERENCES renders(id),
    passed INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY,
    script_id INTEGER REFERENCES scripts(id),
    category TEXT NOT NULL,                        -- llm | tts | higgsfield | other
    usd REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_cost(
    conn: sqlite3.Connection, category: str, usd: float, detail: str, script_id: int | None
) -> None:
    conn.execute(
        "INSERT INTO costs (script_id, category, usd, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (script_id, category, round(usd, 6), detail, now()),
    )
