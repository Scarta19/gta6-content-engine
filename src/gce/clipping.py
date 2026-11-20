"""Clipping mode for paid clip campaigns (e.g. Whop Content Rewards).

Only footage a campaign owner provides or authorizes is ingested (`source_url` is mandatory and
the operator must pass --authorized). Pipeline: transcribe (local faster-whisper, optional) ->
find moments (LLM over the timestamped transcript, or by hand) -> cut 9:16 with blurred fill,
burned captions and a hook -> human approve -> post manually on the campaign's account ->
record the post URL and views -> estimate payouts per campaign rules.

Submission to Content Rewards stays manual: its documented flow is pasting the posted video's
link in the campaign page, and Whop's public bounty-submission API needs a user OAuth token and
is not documented as the Content Rewards flow.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gce import db, media, subtitles
from gce.config import Config, LlmCfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL DEFAULT '',
    rate_per_1k REAL NOT NULL,             -- USD per 1,000 views
    min_payout REAL NOT NULL DEFAULT 0,    -- a post earns nothing until it reaches this
    max_payout REAL NOT NULL DEFAULT 0,    -- per-post cap; 0 = no cap
    fee_pct REAL NOT NULL DEFAULT 7,       -- platform fee on clipper payouts
    platforms TEXT NOT NULL DEFAULT 'tiktok youtube instagram',
    rules TEXT NOT NULL DEFAULT '',
    required_text TEXT NOT NULL DEFAULT '', -- tags/mentions the campaign requires in captions
    watermark TEXT NOT NULL DEFAULT '',     -- handle of the dedicated clip account, or ''
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_sources (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    path TEXT NOT NULL,
    source_url TEXT NOT NULL,               -- where the campaign provides/authorizes it
    duration_s REAL NOT NULL DEFAULT 0,
    transcript_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES clip_sources(id),
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    hook TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | rendered | approved | rejected
    reviewer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    submitted INTEGER NOT NULL DEFAULT 0,   -- link submitted on the campaign page
    views INTEGER NOT NULL DEFAULT 0,
    views_at TEXT,
    created_at TEXT NOT NULL
);
"""

MIN_CLIP_S = 8.0
MAX_CLIP_S = 90.0


# Columns added after the first release of clipping mode.
COLUMNS = (
    ("campaigns", "kind", "TEXT NOT NULL DEFAULT 'rate'"),  # rate | pool
    ("campaigns", "pool_usd", "REAL NOT NULL DEFAULT 0"),
    ("campaigns", "min_views", "INTEGER NOT NULL DEFAULT 0"),  # a post counts only above this
    # Campaign-provided overlay (PNG with transparency) that every clip must carry.
    ("campaigns", "overlay_path", "TEXT NOT NULL DEFAULT ''"),
)


def set_overlay(conn: sqlite3.Connection, campaign_id: int, path: Path | None) -> None:
    if path is not None and not path.exists():
        raise FileNotFoundError(path)
    conn.execute(
        "UPDATE campaigns SET overlay_path = ? WHERE id = ?",
        (str(path.resolve()) if path else "", campaign_id),
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, ddl in COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# --- campaigns -------------------------------------------------------------------------------


def add_campaign(
    conn: sqlite3.Connection,
    name: str,
    rate_per_1k: float,
    *,
    url: str = "",
    min_payout: float = 0.0,
    max_payout: float = 0.0,
    fee_pct: float = 7.0,
    platforms: str = "tiktok youtube instagram",
    rules: str = "",
    required_text: str = "",
    watermark: str = "",
    pool_usd: float = 0.0,
    min_views: int = 0,
) -> int:
    if pool_usd <= 0 and rate_per_1k <= 0:
        raise ValueError("give a rate per 1K views, or a pool for pool campaigns")
    cur = conn.execute(
        "INSERT INTO campaigns (name, url, rate_per_1k, min_payout, max_payout, fee_pct,"
        " platforms, rules, required_text, watermark, created_at, kind, pool_usd, min_views)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            url,
            rate_per_1k,
            min_payout,
            max_payout,
            fee_pct,
            platforms,
            rules,
            required_text,
            watermark,
            db.now(),
            "pool" if pool_usd > 0 else "rate",
            pool_usd,
            min_views,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def campaign(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM campaigns WHERE id = ? OR name = ?", (str(ref), str(ref))
    ).fetchone()
    if row is None:
        raise ValueError(f"campaign {ref!r} not found")
    return row


# --- sources ---------------------------------------------------------------------------------


def download(url: str, out_dir: Path) -> Path:
    """Fetch a campaign-provided video with yt-dlp (must be installed separately)."""
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp not found. `brew install yt-dlp`, or download it yourself.")
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            exe,
            "-f",
            "bv*[height<=1080]+ba/b[height<=1080]",
            "--merge-output-format",
            "mp4",
            "-o",
            str(out_dir / "%(id)s.%(ext)s"),
            "--print",
            "after_move:filepath",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()[-800:]}")
    return Path(proc.stdout.strip().splitlines()[-1])


def add_source(
    conn: sqlite3.Connection, campaign_id: int, path: Path, source_url: str, authorized: bool
) -> int:
    if not authorized:
        raise PermissionError(
            "only footage the campaign provides or authorizes may be clipped; pass --authorized "
            "after checking the campaign page lists this source"
        )
    if not source_url.strip():
        raise ValueError("source_url is required (the campaign's link to this footage)")
    if not path.exists():
        raise FileNotFoundError(path)
    cur = conn.execute(
        "INSERT INTO clip_sources (campaign_id, path, source_url, duration_s, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (campaign_id, str(path.resolve()), source_url, media.duration(path), db.now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM clip_sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"source {source_id} not found")
    return row


# --- transcript ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def transcribe(
    conn: sqlite3.Connection, source_id: int, model: str = "small", language: str | None = None
) -> int:
    """Word-level transcript with faster-whisper (local, free). Returns the word count."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed: `uv pip install faster-whisper` (runs locally)"
        ) from exc
    src = _source(conn, source_id)
    wm = WhisperModel(model, device="auto", compute_type="int8")
    segments, _info = wm.transcribe(src["path"], word_timestamps=True, language=language)
    words = [
        {"start": round(w.start, 2), "end": round(w.end, 2), "text": w.word.strip()}
        for seg in segments
        for w in (seg.words or [])
    ]
    save_transcript(conn, source_id, words)
    return len(words)


def save_transcript(conn: sqlite3.Connection, source_id: int, words: list[dict[str, Any]]) -> None:
    conn.execute(
        "UPDATE clip_sources SET transcript_json = ? WHERE id = ?", (json.dumps(words), source_id)
    )


def words_of(conn: sqlite3.Connection, source_id: int) -> list[Word]:
    raw = _source(conn, source_id)["transcript_json"]
    if not raw:
        raise ValueError(f"source {source_id} has no transcript; run `gce clip transcribe`")
    return [Word(float(w["start"]), float(w["end"]), str(w["text"])) for w in json.loads(raw)]


def transcript_lines(words: list[Word], every_s: float = 6.0) -> list[str]:
    """Compact '[mm:ss] text' lines for the moment finder."""
    lines: list[str] = []
    buf: list[str] = []
    t0 = words[0].start if words else 0.0
    for w in words:
        if buf and w.start - t0 >= every_s:
            lines.append(f"[{t0:.1f}] {' '.join(buf)}")
            buf, t0 = [], w.start
        buf.append(w.text)
    if buf:
        lines.append(f"[{t0:.1f}] {' '.join(buf)}")
    return lines


# --- moments ---------------------------------------------------------------------------------

FINDER_SYSTEM = """You pick the most viral moments from a creator's video for short vertical
clips (TikTok, Shorts, Reels), for a paid clipping campaign the creator runs.
A great clip: grabs in the first second (a reaction, a bold claim, a question, conflict, a
punchline), is self-contained, and ends on a payoff. Never pick moments that break the campaign
rules. Never invent what was said: hooks paraphrase or quote the transcript."""


def find_prompt(
    words: list[Word], rules: str, n: int, min_s: float, max_s: float, required: str
) -> str:
    return "\n".join(
        [
            f"Campaign rules: {rules or 'none given'}",
            f"Required caption text: {required or 'none'}",
            f"Return the {n} best clips, each {min_s:.0f}-{max_s:.0f} seconds long.",
            "Transcript with start times in seconds:",
            *transcript_lines(words),
            "",
            'Reply with JSON only: {"clips": [{"start": s, "end": s, "hook": "<=10 words on-'
            'screen hook", "title": "post title", "reason": "why it will retain", "score": 0-10}]}',
        ]
    )


def save_clip(
    conn: sqlite3.Connection,
    source_id: int,
    start: float,
    end: float,
    hook: str = "",
    title: str = "",
    reason: str = "",
    score: float = 0.0,
) -> int:
    src = _source(conn, source_id)
    if src["duration_s"] and end > src["duration_s"] + 0.5:
        raise ValueError(f"clip ends at {end:.1f}s, after the source ({src['duration_s']:.1f}s)")
    if not MIN_CLIP_S <= end - start <= MAX_CLIP_S:
        raise ValueError(f"clip length {end - start:.1f}s outside {MIN_CLIP_S}-{MAX_CLIP_S}s")
    cur = conn.execute(
        "INSERT INTO clips (source_id, start_s, end_s, hook, title, reason, score, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, start, end, hook, title, reason, score, db.now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def snap_to_words(words: list[Word], start: float, end: float) -> tuple[float, float]:
    """Move edges to word boundaries so clips never cut mid-word."""
    starts = [w.start for w in words if w.start <= start + 0.01]
    ends = [w.end for w in words if w.end >= end - 0.01]
    s = max(starts) if starts else start
    e = min(ends) if ends else end
    return round(max(0.0, s - 0.15), 2), round(e + 0.25, 2)


def find_moments(
    conn: sqlite3.Connection,
    llm_cfg: LlmCfg,
    source_id: int,
    n: int = 5,
    min_s: float = 20.0,
    max_s: float = 60.0,
) -> list[int]:
    from gce.llm import complete
    from gce.scripting import extract_json

    src = _source(conn, source_id)
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (src["campaign_id"],)).fetchone()
    words = words_of(conn, source_id)
    result = complete(
        llm_cfg,
        FINDER_SYSTEM,
        find_prompt(words, camp["rules"], n, min_s, max_s, camp["required_text"]),
    )
    db.record_cost(conn, "llm", result.usd, f"clip finder source {source_id}", None)
    ids: list[int] = []
    for c in extract_json(result.text).get("clips", []):
        try:
            s, e = snap_to_words(words, float(c["start"]), float(c["end"]))
            ids.append(
                save_clip(
                    conn,
                    source_id,
                    s,
                    e,
                    str(c.get("hook", ""))[:120],
                    str(c.get("title", ""))[:150],
                    str(c.get("reason", ""))[:500],
                    float(c.get("score", 0) or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed or out-of-bounds picks rather than inventing fixes
    return ids


# --- render ----------------------------------------------------------------------------------


def clip_cues(words: list[Word], start: float, end: float, max_words: int) -> list[subtitles.Cue]:
    """Caption chunks timed to the real words inside [start, end], shifted to clip time."""
    inside = [w for w in words if w.start >= start - 0.05 and w.end <= end + 0.05]
    cues: list[subtitles.Cue] = []
    buf: list[Word] = []
    for w in inside:
        buf.append(w)
        if len(buf) >= max_words or re.search(r"[.!?,;:]$", w.text):
            cues.append(
                subtitles.Cue(
                    round(buf[0].start - start, 3), round(buf[-1].end - start, 3), _join(buf)
                )
            )
            buf = []
    if buf:
        cues.append(
            subtitles.Cue(round(buf[0].start - start, 3), round(buf[-1].end - start, 3), _join(buf))
        )
    return cues


def _join(ws: list[Word]) -> str:
    return " ".join(w.text for w in ws)


def render_clip(conn: sqlite3.Connection, cfg: Config, clip_id: int) -> Path:
    media.require_libass()
    clip = _clip(conn, clip_id)
    src = _source(conn, clip["source_id"])
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (src["campaign_id"],)).fetchone()
    try:
        words = words_of(conn, src["id"])
    except ValueError:
        words = []
    length = clip["end_s"] - clip["start_s"]
    work = cfg.paths.work_dir / f"clip_{clip_id}"
    work.mkdir(parents=True, exist_ok=True)
    # Never brand campaign clips with the faceless channel's handle.
    rcfg = dataclasses.replace(cfg.render, watermark=camp["watermark"])
    cues = clip_cues(words, clip["start_s"], clip["end_s"], rcfg.max_words_per_caption)
    # Caption fonts have no emoji glyphs (they render as boxes); keep emoji for the post text.
    no_emoji = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]", "", clip["hook"] or "")
    hook = " ".join(no_emoji.split())
    subtitles.write_ass(cues, work / "captions.ass", rcfg, hook, [], length, False)
    w, h = rcfg.width, rcfg.height
    overlay = camp["overlay_path"]
    # The campaign logo sits in the blurred gap between the hook and the 16:9 gameplay strip:
    # inside the TikTok/Reels safe zone (the bottom ~420px is covered by app UI) and never
    # over the stream, the hook or the captions.
    strip_top = (h - w * 9 // 16) // 2
    # Logos often ship with transparent padding, so size by width and centre the image on
    # the gap: the visible mark lands between the hook and the strip.
    logo_w = int(w * 0.8) // 2 * 2
    branded = (
        f"[base];[1:v]scale={logo_w}:-2,format=rgba[ov];"
        f"[base][ov]overlay=(W-w)/2:{strip_top - 85}-h/2:shortest=1,"
        if overlay
        else ","
    )
    vf = (
        f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=24:2,eq=brightness=-0.08[bg];"
        f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{branded}"
        f"ass=captions.ass,drawbox=x=0:y=ih-14:w=iw*t/{length:.3f}:h=14:"
        "color=0xFFE500@0.95:t=fill[v]"
    )
    overlay_input = ["-loop", "1", "-i", overlay] if overlay else []
    out = work / f"clip_{clip_id}.mp4"
    media.run(
        [
            "-ss",
            f"{clip['start_s']:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            src["path"],
            *overlay_input,
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(rcfg.fps),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            out.name,
        ],
        cwd=work,
    )
    caption = " ".join(x for x in (clip["title"], camp["required_text"]) if x).strip()
    (work / "caption.txt").write_text(caption + "\n", encoding="utf-8")
    conn.execute(
        "UPDATE clips SET path = ?, status = 'rendered' WHERE id = ?", (str(out.resolve()), clip_id)
    )
    return out


def _clip(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"clip {clip_id} not found")
    return row


# --- review, posting, payouts ----------------------------------------------------------------


def decide(conn: sqlite3.Connection, clip_id: int, reviewer: str, approve: bool) -> str:
    clip = _clip(conn, clip_id)
    if clip["status"] not in ("rendered", "approved", "rejected"):
        raise ValueError("render the clip and watch it before deciding")
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE clips SET status = ?, reviewer = ? WHERE id = ?", (status, reviewer, clip_id)
    )
    return status


def record_post(
    conn: sqlite3.Connection, clip_id: int, platform: str, url: str, submitted: bool = False
) -> int:
    clip = _clip(conn, clip_id)
    if clip["status"] != "approved":
        raise PermissionError(f"clip {clip_id} is {clip['status']}; approve it first")
    camp = conn.execute(
        "SELECT c.platforms FROM campaigns c JOIN clip_sources s ON s.campaign_id = c.id"
        " WHERE s.id = ?",
        (clip["source_id"],),
    ).fetchone()
    if platform not in camp["platforms"].split():
        raise ValueError(f"campaign accepts {camp['platforms']}, not {platform}")
    cur = conn.execute(
        "INSERT INTO clip_posts (clip_id, platform, url, submitted, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (clip_id, platform, url, int(submitted), db.now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


ZERNIO_PLATFORMS = ("tiktok", "instagram", "youtube")


def publish_clip(
    conn: sqlite3.Connection,
    cfg: Config,
    clip_id: int,
    platforms: list[str],
    *,
    live: bool = False,
    scheduled_for: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Post an approved, rendered clip through Zernio (official platform APIs).

    Dry run by default. Live needs an approved clip and GCE_ALLOW_LIVE_PUBLISH=yes. Each
    platform gets a clip_posts row (url "zernio:<post id>", submitted=0) so the campaign
    submission, which stays manual, can be tracked. Campaign clips are real stream footage, so
    the synthetic-media flag is off.
    """
    import os

    from gce import zernio

    clip = _clip(conn, clip_id)
    camp = conn.execute(
        "SELECT c.platforms FROM campaigns c JOIN clip_sources s ON s.campaign_id = c.id"
        " WHERE s.id = ?",
        (clip["source_id"],),
    ).fetchone()
    bad = [p for p in platforms if p not in ZERNIO_PLATFORMS or p not in camp["platforms"].split()]
    if bad:
        raise ValueError(f"campaign accepts {camp['platforms']}; cannot publish to {bad}")
    if not clip["path"] or not Path(clip["path"]).exists():
        raise FileNotFoundError(f"clip {clip_id} is not rendered; run `gce clip render` first")
    caption = (Path(clip["path"]).parent / "caption.txt").read_text(encoding="utf-8").strip()
    if live:
        if clip["status"] != "approved":
            raise PermissionError(f"clip {clip_id} is {clip['status']}; approve it first")
        if os.environ.get("GCE_ALLOW_LIVE_PUBLISH") != "yes":
            raise PermissionError("set GCE_ALLOW_LIVE_PUBLISH=yes to enable live publishing")
    accounts = (
        cfg.zernio.accounts
        if live
        else {p: cfg.zernio.accounts.get(p) or "<account id>" for p in platforms}
    )
    body = zernio.build_post_body(
        caption=caption,
        media_url="<uploaded at publish time>",
        platforms=platforms,
        accounts=accounts,
        timezone=cfg.zernio.timezone,
        scheduled_for=scheduled_for,
        tiktok_privacy="PUBLIC_TO_EVERYONE",
        title=clip["title"] or clip["hook"],
        tags=re.findall(r"#(\w+)", caption),
        ai_generated=False,
    )
    if not live:
        return {"mode": "dry_run", "request": body}
    zc = client or zernio.Client(cfg.zernio)
    body["mediaItems"][0]["url"] = zc.upload(Path(clip["path"]))
    resp = zc.create_post(body)
    post = resp.get("post", resp)
    post_id = str(post.get("_id", ""))
    if not post_id:
        raise zernio.ZernioError(f"Zernio response had no post id: {json.dumps(resp)[:500]}")
    ids = [record_post(conn, clip_id, p, f"zernio:{post_id}") for p in platforms]
    return {"mode": "live", "post_id": post_id, "clip_posts": ids}


def unsubmitted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Posts not yet submitted on their campaign (Content Rewards wants this within 30 min)."""
    return conn.execute(
        "SELECT p.*, k.title FROM clip_posts p JOIN clips k ON k.id = p.clip_id"
        " WHERE p.submitted = 0 ORDER BY p.created_at"
    ).fetchall()


def set_views(conn: sqlite3.Connection, post_id: int, views: int, submitted: bool | None) -> None:
    conn.execute(
        "UPDATE clip_posts SET views = ?, views_at = ?,"
        " submitted = COALESCE(?, submitted) WHERE id = ?",
        (views, db.now(), None if submitted is None else int(submitted), post_id),
    )


def payout(views: int, rate_per_1k: float, min_payout: float, max_payout: float) -> float:
    """Gross USD a post earns under view-based campaign rules."""
    gross = views / 1000 * rate_per_1k
    if gross < min_payout:
        return 0.0
    return round(min(gross, max_payout) if max_payout else gross, 2)


def report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in conn.execute("SELECT * FROM campaigns ORDER BY id"):
        posts = conn.execute(
            "SELECT p.* FROM clip_posts p JOIN clips k ON k.id = p.clip_id"
            " JOIN clip_sources s ON s.id = k.source_id WHERE s.campaign_id = ?",
            (c["id"],),
        ).fetchall()
        clips = conn.execute(
            "SELECT status, COUNT(*) n FROM clips k JOIN clip_sources s ON s.id = k.source_id"
            " WHERE s.campaign_id = ? GROUP BY status",
            (c["id"],),
        ).fetchall()
        counted = [p for p in posts if p["submitted"] and p["views"] >= c["min_views"]]
        if c["kind"] == "pool":
            # Pool payouts depend on every clipper's views, which we can't see: report our
            # qualifying views; the share is known only when the campaign settles.
            gross = 0.0
        else:
            gross = sum(
                payout(p["views"], c["rate_per_1k"], c["min_payout"], c["max_payout"])
                for p in counted
            )
        out.append(
            {
                "campaign": c["name"],
                "kind": c["kind"],
                "pool_usd": c["pool_usd"],
                "qualifying_posts": len(counted),
                "qualifying_views": sum(p["views"] for p in counted),
                "clips": {r["status"]: r["n"] for r in clips},
                "posts": len(posts),
                "submitted": sum(1 for p in posts if p["submitted"]),
                "views": sum(p["views"] for p in posts),
                "est_gross_usd": round(gross, 2),
                "est_net_usd": round(gross * (1 - c["fee_pct"] / 100), 2),
            }
        )
    return out
