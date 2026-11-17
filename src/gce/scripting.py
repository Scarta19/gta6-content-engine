"""Script generation and validation for the three launch formats."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from gce import db, verify
from gce.config import LlmCfg
from gce.llm import complete

FORMATS = ("news", "analysis", "concept")
AI_DISCLOSURE_RE = re.compile(r"\b(ai[- ]generated|ai concept|made with ai|fan concept)\b", re.I)
HYPE_WITHOUT_PROOF = re.compile(
    r"\b(leak(ed)? confirms|officially confirmed|rockstar (just )?confirmed)\b", re.I
)

SYSTEM_PROMPT = """You write 30-50 second vertical short-form video scripts in American English
for a GTA VI news and analysis channel aimed at US gamers aged 16-34.

Hard rules:
- Never invent Rockstar announcements, gameplay, features, prices, dates or leaks.
- Every factual claim must cite one of the provided source URLs and keep its verification status.
- CONFIRMED stories may be stated as fact and attributed to Rockstar.
- CORROBORATED stories must be attributed ("Bloomberg reports...").
- UNVERIFIED material may only appear as clearly labeled speculation or rumor.
- Concept videos are original AI-generated visuals: say so in the voiceover and caption.
- Hook: max 12 words, spoken in the first 2 seconds, creates curiosity without lying.
- One clear CTA (follow, comment a specific answer, or share).

Return ONLY a JSON object with keys:
title, hook, voiceover, on_screen (array of short text beats), cta, caption,
hashtags (array, max 5), visual_plan, claims (array of {claim, source_url, status}),
rights_notes."""

FORMAT_BRIEFS = {
    "news": "Format: verified news update. Lead with the single most important new fact.",
    "analysis": (
        "Format: transformative analysis/commentary. Add original insight (comparison, "
        "implication, prediction clearly marked as opinion). Do not just restate the news."
    ),
    "concept": (
        "Format: original AI-generated cinematic concept inspired by confirmed setting details. "
        "Describe 3-5 shots for an AI video tool in visual_plan. Disclose it is an AI concept."
    ),
}


@dataclass
class Script:
    format: str
    title: str
    hook: str
    voiceover: str
    cta: str
    caption: str
    on_screen: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    visual_plan: str = ""
    claims: list[dict[str, str]] = field(default_factory=list)
    rights_notes: str = ""
    story_id: int | None = None

    @property
    def ai_generated(self) -> bool:
        return self.format == "concept"

    @classmethod
    def from_dict(cls, data: dict[str, Any], fmt: str, story_id: int | None) -> Script:
        required = ("title", "hook", "voiceover", "cta", "caption")
        missing = [k for k in required if not str(data.get(k, "")).strip()]
        if missing:
            raise ValueError(f"script missing fields: {', '.join(missing)}")
        return cls(
            format=fmt,
            title=str(data["title"]).strip(),
            hook=str(data["hook"]).strip(),
            voiceover=str(data["voiceover"]).strip(),
            cta=str(data["cta"]).strip(),
            caption=str(data["caption"]).strip(),
            on_screen=[str(x) for x in data.get("on_screen", [])],
            hashtags=[str(x).lstrip("#") for x in data.get("hashtags", [])][:5],
            visual_plan=str(data.get("visual_plan", "")),
            claims=[dict(c) for c in data.get("claims", [])],
            rights_notes=str(data.get("rights_notes", "")),
            story_id=story_id,
        )


def extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("model did not return JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("model JSON is not an object")
    return data


def validate(script: Script, story_status: str | None, source_urls: set[str]) -> list[str]:
    """Return a list of problems. Empty list means the script passes automated checks."""
    problems: list[str] = []
    if script.format not in FORMATS:
        problems.append(f"unknown format {script.format}")
    if len(script.hook.split()) > 12:
        problems.append("hook is longer than 12 words")
    words = len(script.voiceover.split())
    if not 60 <= words <= 160:
        problems.append(f"voiceover is {words} words; target 60-160 (~25-55s)")
    if script.format == "news" and story_status not in ("confirmed", "corroborated"):
        problems.append("news format requires a confirmed or corroborated story")
    if script.format in ("news", "analysis") and not script.claims:
        problems.append("news/analysis scripts must list their factual claims with sources")
    for claim in script.claims:
        url = claim.get("source_url", "")
        if source_urls and url not in source_urls:
            problems.append(f"claim cites a URL not in the story sources: {url!r}")
        if claim.get("status") == "unverified" and "rumor" not in script.voiceover.lower():
            problems.append("unverified claim used without saying 'rumor' in the voiceover")
    if story_status != "confirmed" and HYPE_WITHOUT_PROOF.search(script.voiceover):
        problems.append("voiceover says 'confirmed'/'leak confirms' but the story is not confirmed")
    if script.ai_generated and not (
        AI_DISCLOSURE_RE.search(script.voiceover) and AI_DISCLOSURE_RE.search(script.caption)
    ):
        problems.append("concept videos must disclose AI generation in voiceover and caption")
    return problems


def build_prompt(fmt: str, story: sqlite3.Row | None, sources: list[sqlite3.Row]) -> str:
    lines = [FORMAT_BRIEFS[fmt], ""]
    if story is not None:
        lines += [f"Story: {story['title']}", f"Verification status: {story['status'].upper()}"]
    lines.append("Sources (tier, url, headline):")
    for s in sources:
        lines.append(f"- [{s['tier']}] {s['url']} :: {s['title']}")
    if not sources:
        lines.append("- none (concept only: do not make factual claims)")
    return "\n".join(lines)


def generate(
    conn: sqlite3.Connection, cfg: LlmCfg, fmt: str, story_id: int | None
) -> tuple[int, list[str]]:
    story = None
    sources: list[sqlite3.Row] = []
    if story_id is not None:
        story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if story is None:
            raise ValueError(f"story {story_id} not found")
        sources = verify.sources_for(conn, story_id)
    result = complete(cfg, SYSTEM_PROMPT, build_prompt(fmt, story, sources))
    script = Script.from_dict(extract_json(result.text), fmt, story_id)
    script_id = save(conn, script, model=cfg.model)
    db.record_cost(
        conn,
        "llm",
        result.usd,
        f"{cfg.model} in={result.input_tokens} out={result.output_tokens}",
        script_id,
    )
    return script_id, check(conn, script_id)


def save(conn: sqlite3.Connection, script: Script, model: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO scripts (story_id, format, title, hook, voiceover, on_screen_json, cta,"
        " caption, hashtags, visual_plan, claims_json, rights_notes, ai_generated, model,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            script.story_id,
            script.format,
            script.title,
            script.hook,
            script.voiceover,
            json.dumps(script.on_screen),
            script.cta,
            script.caption,
            " ".join(script.hashtags),
            script.visual_plan,
            json.dumps(script.claims),
            script.rights_notes,
            int(script.ai_generated),
            model,
            db.now(),
        ),
    )
    if script.story_id is not None:
        conn.execute("UPDATE stories SET used = 1 WHERE id = ?", (script.story_id,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def load(conn: sqlite3.Connection, script_id: int) -> Script:
    row = conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    if row is None:
        raise ValueError(f"script {script_id} not found")
    return Script(
        format=row["format"],
        title=row["title"],
        hook=row["hook"],
        voiceover=row["voiceover"],
        cta=row["cta"],
        caption=row["caption"],
        on_screen=json.loads(row["on_screen_json"]),
        hashtags=row["hashtags"].split(),
        visual_plan=row["visual_plan"],
        claims=json.loads(row["claims_json"]),
        rights_notes=row["rights_notes"],
        story_id=row["story_id"],
    )


def check(conn: sqlite3.Connection, script_id: int) -> list[str]:
    script = load(conn, script_id)
    status = None
    urls: set[str] = set()
    if script.story_id is not None:
        story = conn.execute(
            "SELECT status FROM stories WHERE id = ?", (script.story_id,)
        ).fetchone()
        status = story["status"] if story else None
        urls = {r["url"] for r in verify.sources_for(conn, script.story_id)}
    else:
        # Hand-written scripts without a story: verification status comes from their claims.
        statuses = {c.get("status", "unverified") for c in script.claims}
        status = "confirmed" if statuses == {"confirmed"} else None
    return validate(script, status, urls)


def to_json(script: Script) -> str:
    return json.dumps(asdict(script), indent=2)
