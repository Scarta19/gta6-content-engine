"""Metrics capture (Zernio sync or manual entry) and the cost/performance report."""

from __future__ import annotations

import sqlite3
from typing import Any

from gce import db, zernio
from gce.config import Config

METRIC_FIELDS = ("views", "impressions", "reach", "likes", "comments", "shares", "saves")


def add(conn: sqlite3.Connection, publication_id: int, values: dict[str, Any]) -> None:
    row = {k: int(values.get(k) or 0) for k in METRIC_FIELDS}
    rate = float(values.get("engagementRate") or values.get("engagement_rate") or 0)
    conn.execute(
        "INSERT INTO metrics (publication_id, captured_at, views, impressions, reach, likes,"
        " comments, shares, saves, engagement_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (publication_id, db.now(), *row.values(), rate),
    )


def sync(conn: sqlite3.Connection, cfg: Config, client: zernio.Client | None = None) -> int:
    pubs = conn.execute(
        "SELECT id, external_id FROM publications WHERE mode IN ('live', 'manual')"
        " AND external_id != ''"
    ).fetchall()
    if not pubs:
        return 0
    zc = client or zernio.Client(cfg.zernio)
    n = 0
    for p in pubs:
        metrics = zernio.extract_metrics(zc.analytics(p["external_id"]))
        if metrics is not None:
            add(conn, p["id"], metrics)
            n += 1
    return n


def report(conn: sqlite3.Connection, cfg: Config) -> list[dict[str, Any]]:
    published = conn.execute(
        "SELECT COUNT(DISTINCT render_id) FROM publications WHERE mode IN ('live', 'manual')"
    ).fetchone()[0]
    fixed_share = cfg.costs.monthly_fixed_usd / published if published else 0.0
    rows = conn.execute(
        """
        SELECT r.id AS render_id, s.id AS script_id, s.format, s.title, r.status,
               (SELECT COALESCE(SUM(usd), 0) FROM costs c WHERE c.script_id = s.id) AS var_usd
        FROM renders r JOIN scripts s ON s.id = r.script_id
        ORDER BY r.id
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        latest = conn.execute(
            """
            SELECT p.platform, m.views, m.likes, m.comments, m.shares, m.saves
            FROM publications p
            JOIN metrics m ON m.id = (
                SELECT id FROM metrics WHERE publication_id = p.id ORDER BY id DESC LIMIT 1)
            WHERE p.render_id = ?
            """,
            (r["render_id"],),
        ).fetchall()
        views = sum(x["views"] for x in latest)
        engaged = sum(x["likes"] + x["comments"] + x["shares"] + x["saves"] for x in latest)
        live = conn.execute(
            "SELECT COUNT(*) FROM publications WHERE render_id = ? AND mode IN ('live','manual')",
            (r["render_id"],),
        ).fetchone()[0]
        total_usd = float(r["var_usd"]) + (fixed_share if live else 0.0)
        by_platform = {x["platform"]: x["views"] for x in latest}
        out.append(
            {
                "render": r["render_id"],
                "format": r["format"],
                "title": r["title"][:40],
                "status": r["status"],
                "cost_usd": round(total_usd, 4),
                "posts": live,
                "usd_per_post": round(total_usd / live, 4) if live else None,
                "views": views,
                "views_by_platform": by_platform,
                "shares": sum(x["shares"] for x in latest),
                "eng_rate": round(engaged / views, 4) if views else 0.0,
                "usd_per_1k_views": round(total_usd / views * 1000, 4) if views else None,
            }
        )
    return out
