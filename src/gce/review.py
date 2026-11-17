"""Human approval gate. Nothing can be published live without an approved review."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from gce import assets, db, gates, scripting

CHECKLIST = (
    ("watched", "I watched the full video with sound, start to finish."),
    ("facts", "Every factual claim matches its cited source and verification status."),
    ("no_fabrication", "Nothing implies an unannounced Rockstar feature, date, price or leak."),
    ("rights", "Every visual and audio asset has documented rights (listed above)."),
    ("ai_label", "AI-generated visuals are labeled on screen and in the caption (or N/A)."),
    ("captions", "Captions are accurate, readable and not covered by platform UI."),
    ("platform", "Title/caption/cover are not misleading; platform AI-content toggle planned."),
)


def summary(conn: sqlite3.Connection, render_id: int) -> dict[str, Any]:
    render = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
    if render is None:
        raise ValueError(f"render {render_id} not found")
    script = scripting.load(conn, render["script_id"])
    story = None
    if script.story_id is not None:
        story = conn.execute(
            "SELECT title, status FROM stories WHERE id = ?", (script.story_id,)
        ).fetchone()
    cost = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM costs WHERE script_id = ?", (render["script_id"],)
    ).fetchone()[0]
    return {
        "render_id": render_id,
        "video": render["path"],
        "duration_s": render["duration_s"],
        "status": render["status"],
        "format": script.format,
        "story": dict(story) if story else None,
        "hook": script.hook,
        "caption": f"{script.caption} " + " ".join(f"#{h}" for h in script.hashtags),
        "claims": script.claims,
        "rights_notes": script.rights_notes,
        "assets": [
            {
                "id": a["id"],
                "path": a["path"],
                "license": a["license"],
                "source": a["source"],
                "provenance_url": a["provenance_url"],
                "ai_generated": bool(a["ai_generated"]),
            }
            for a in assets.for_script(conn, render["script_id"])
        ],
        "automated_check_problems": scripting.check(conn, render["script_id"]),
        "quality_gates_passed": gates.latest_passed(conn, render_id),
        "cost_usd": round(float(cost), 4),
    }


def decide(
    conn: sqlite3.Connection,
    render_id: int,
    reviewer: str,
    answers: dict[str, bool],
    notes: str = "",
) -> str:
    if not reviewer.strip():
        raise ValueError("reviewer name is required")
    info = summary(conn, render_id)
    missing = [k for k, _ in CHECKLIST if k not in answers]
    if missing:
        raise ValueError(f"checklist incomplete: {', '.join(missing)}")
    gates_ok = gates.latest_passed(conn, render_id)
    approved = all(answers.values()) and not info["automated_check_problems"] and gates_ok
    if not gates_ok:
        notes = (notes + " [blocked: quality gates not passed]").strip()
    decision = "approved" if approved else "rejected"
    conn.execute(
        "INSERT INTO reviews (render_id, decision, reviewer, checklist_json, notes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (render_id, decision, reviewer, json.dumps(answers), notes, db.now()),
    )
    conn.execute("UPDATE renders SET status = ? WHERE id = ?", (decision, render_id))
    return decision


def is_approved(conn: sqlite3.Connection, render_id: int) -> bool:
    row = conn.execute("SELECT status FROM renders WHERE id = ?", (render_id,)).fetchone()
    return bool(row and row["status"] == "approved")
