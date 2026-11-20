"""Distribution: dry-run by default, manual export always available, live only when approved."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from gce import assets, db, review, scripting, zernio
from gce.config import Config

PLATFORMS = ("tiktok", "instagram", "facebook", "youtube")
PLACEHOLDER_MEDIA_URL = "<uploaded via POST /media/presign at publish time>"


def caption_for(conn: sqlite3.Connection, script_id: int) -> str:
    s = scripting.load(conn, script_id)
    tags = " ".join(f"#{h}" for h in s.hashtags)
    return f"{s.caption}\n\n{tags}".strip()


def _render(conn: sqlite3.Connection, render_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
    if row is None:
        raise ValueError(f"render {render_id} not found")
    result: sqlite3.Row = row
    return result


def export(conn: sqlite3.Connection, render_id: int, dest: Path) -> Path:
    """Manual-upload package: video, caption, captions file and rights sheet."""
    r = _render(conn, render_id)
    info = review.summary(conn, render_id)
    folder = dest / f"render_{render_id}"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(r["path"], folder / "video.mp4")
    shutil.copy2(r["srt_path"], folder / "captions.srt")
    (folder / "caption.txt").write_text(caption_for(conn, r["script_id"]), encoding="utf-8")
    (folder / "review.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return folder


def publish(
    conn: sqlite3.Connection,
    cfg: Config,
    render_id: int,
    platforms: list[str],
    *,
    live: bool = False,
    scheduled_for: str | None = None,
    tiktok_privacy: str = "PUBLIC_TO_EVERYONE",
    ai_disclosure_confirmed: bool = False,
    client: zernio.Client | None = None,
) -> dict[str, Any]:
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad:
        raise ValueError(f"unsupported platforms: {bad}")
    r = _render(conn, render_id)
    script = scripting.load(conn, r["script_id"])
    caption = caption_for(conn, r["script_id"])
    # AI disclosure covers the script flag AND any AI-generated visual in the cut.
    ai_content = script.ai_generated or any(
        a["ai_generated"] for a in assets.for_script(conn, r["script_id"])
    )

    if live:
        if not review.is_approved(conn, render_id):
            raise PermissionError(f"render {render_id} is not approved. Run `gce review` first.")
        if os.environ.get("GCE_ALLOW_LIVE_PUBLISH") != "yes":
            raise PermissionError("set GCE_ALLOW_LIVE_PUBLISH=yes to enable live publishing")
        if ai_content and not ai_disclosure_confirmed:
            raise PermissionError(
                "AI-generated video: YouTube gets containsSyntheticMedia automatically, but "
                "Zernio has no TikTok AIGC field, so turn on TikTok's 'AI-generated content' "
                "label in the app, then pass --ai-disclosure-confirmed"
            )

    body = zernio.build_post_body(
        caption=caption,
        media_url=PLACEHOLDER_MEDIA_URL,
        platforms=platforms,
        accounts=cfg.zernio.accounts
        if live
        else {p: cfg.zernio.accounts.get(p) or "<account id>" for p in platforms},
        timezone=cfg.zernio.timezone,
        scheduled_for=scheduled_for,
        tiktok_privacy=tiktok_privacy,
        title=script.title,
        tags=script.hashtags,
        ai_generated=ai_content,
    )
    if not live:
        for p in platforms:
            conn.execute(
                "INSERT INTO publications (render_id, platform, mode, status, request_json,"
                " created_at) VALUES (?, ?, 'dry_run', 'not_sent', ?, ?)",
                (render_id, p, json.dumps(body), db.now()),
            )
        return {"mode": "dry_run", "request": body}

    zc = client or zernio.Client(cfg.zernio)
    body["mediaItems"][0]["url"] = zc.upload(Path(r["path"]))
    resp = zc.create_post(body)
    post = resp.get("post", resp)
    post_id = str(post.get("_id", ""))
    if not post_id:
        raise zernio.ZernioError(f"Zernio response had no post id: {json.dumps(resp)[:500]}")
    for p in platforms:
        conn.execute(
            "INSERT INTO publications (render_id, platform, mode, external_id, status,"
            " request_json, response_json, created_at) VALUES (?, ?, 'live', ?, ?, ?, ?, ?)",
            (
                render_id,
                p,
                post_id,
                str(post.get("status", "submitted")),
                json.dumps(body),
                json.dumps(resp),
                db.now(),
            ),
        )
    return {"mode": "live", "post_id": post_id, "status": post.get("status")}


def record_manual(conn: sqlite3.Connection, render_id: int, platform: str, external_id: str) -> int:
    """Record a post uploaded by hand so metrics can be tracked against it."""
    _render(conn, render_id)
    if not review.is_approved(conn, render_id):
        raise PermissionError(f"render {render_id} is not approved")
    cur = conn.execute(
        "INSERT INTO publications (render_id, platform, mode, external_id, status, created_at)"
        " VALUES (?, ?, 'manual', ?, 'published', ?)",
        (render_id, platform, external_id, db.now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid
