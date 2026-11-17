"""Pre-approval quality gates. All three must pass before a human can approve a render.

1. facts          every claim cites a story source and keeps its verification status
2. rights_safety  licensed assets only, no Rockstar-owned media, AI labeled, no spoilers after
                  launch without a warning, no kid-directed wording (GTA VI is rated M 17+)
3. virality       hook in the first 2 s, on-screen text by 0.5 s, 30-58 s, burned-in captions,
                  exactly one CTA, plus a hook score (heuristic, optionally LLM with 2 alternatives)
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gce import assets, db, scripting
from gce.config import Config
from gce.llm import complete

KID_DIRECTED = re.compile(
    r"\b(kids?|children|child|little ones|boys and girls|ask your (mom|dad|parents)|"
    r"after school|homework|elementary|middle school|grade school|tweens?|under 13|"
    r"for ages? \d{1,2}\b)",
    re.I,
)
SPOILER_WARNING = re.compile(r"\bspoiler", re.I)
ROCKSTAR_MARKERS = re.compile(r"rockstar|take[- ]?two|gta[ _-]?(vi|6)[ _-]?(trailer|logo)", re.I)
CTA_VERBS = re.compile(r"\b(follow|comment|share|save|subscribe|tag|tell us|drop|like)\b", re.I)

HOOK_POWER = {
    "nobody": 1.0,
    "secret": 1.0,
    "hidden": 1.0,
    "missed": 1.0,
    "why": 0.8,
    "how": 0.5,
    "never": 0.8,
    "biggest": 0.8,
    "first": 0.6,
    "only": 0.6,
    "wrong": 1.0,
    "actually": 0.6,
    "real": 0.5,
    "not": 0.5,
    "this": 0.4,
    "you": 0.8,
    "your": 0.8,
    "stop": 0.8,
    "wait": 0.8,
}

HOOK_SYSTEM = """You rate short-form video hooks for adult (18-34) US gamers on TikTok/Reels.
Score 0-10 for scroll-stopping power WITHOUT clickbait lies. Return ONLY JSON:
{"score": number, "reason": string, "alternatives": [string, string]}.
Alternatives: max 12 words each, truthful to the script, no kid-directed wording."""


@dataclass
class GateResult:
    name: str
    passed: bool
    issues: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    render_id: int
    gates: list[GateResult]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "render_id": self.render_id,
            "passed": self.passed,
            "gates": [asdict(g) for g in self.gates],
        }


def facts_gate(conn: sqlite3.Connection, script_id: int) -> GateResult:
    problems = scripting.check(conn, script_id)
    script = scripting.load(conn, script_id)
    for c in script.claims:
        if not c.get("source_url"):
            problems.append(f"claim without source: {c.get('claim', '')!r}")
    return GateResult("facts", not problems, problems, {"claims": len(script.claims)})


def _launched(launch_date: str, today: date | None = None) -> bool:
    today = today or datetime.now(UTC).date()
    return today >= date.fromisoformat(launch_date)


def rights_safety_gate(
    conn: sqlite3.Connection, cfg: Config, script_id: int, today: date | None = None
) -> GateResult:
    script = scripting.load(conn, script_id)
    issues: list[str] = []
    used = assets.for_script(conn, script_id)
    for a in used:
        if a["license"] not in assets.LICENSES:
            issues.append(f"asset {a['id']} has unknown license {a['license']!r}")
        marker_text = f"{a['source']} {a['provenance_url']} {Path(a['path']).name}"
        if not cfg.gates.allow_rockstar_assets and ROCKSTAR_MARKERS.search(marker_text):
            issues.append(
                f"asset {a['id']} looks Rockstar-owned ({a['source']}); "
                "use owned/AI visuals or enable allow_rockstar_assets after legal review"
            )
    has_ai = script.ai_generated or any(a["ai_generated"] for a in used)
    if has_ai and not (scripting.AI_DISCLOSURE_RE.search(script.caption)):
        issues.append("AI visuals present but caption has no AI disclosure")
    text = " ".join(
        [
            script.hook,
            script.voiceover,
            script.cta,
            script.caption,
            *script.on_screen,
            *script.hashtags,
        ]
    )
    kid = sorted({m.group(0).lower() for m in KID_DIRECTED.finditer(text)})
    if kid:
        issues.append(f"kid-directed wording (audience must be adults, GTA VI is M 17+): {kid}")
    if _launched(cfg.gates.launch_date, today):
        lowered = text.lower()
        hits = [t for t in cfg.gates.spoiler_terms if t.lower() in lowered]
        if hits and not SPOILER_WARNING.search(f"{script.hook} {script.caption}"):
            issues.append(f"possible spoilers after launch without a warning: {hits}")
    return GateResult(
        "rights_safety", not issues, issues, {"assets": len(used), "ai_visuals": has_ai}
    )


def heuristic_hook_score(hook: str) -> float:
    words = re.findall(r"[a-z0-9']+", hook.lower())
    if not words:
        return 0.0
    score = 4.0
    score += min(3.0, sum(HOOK_POWER.get(w, 0) for w in words))
    if any(ch.isdigit() for ch in hook):
        score += 0.8
    if hook.strip().endswith("?"):
        score += 0.5
    n = len(words)
    score += 1.5 if 5 <= n <= 10 else (0.5 if n <= 12 else -2.0)
    if re.search(r"\b(hey guys|welcome back|in this video|today we)\b", hook, re.I):
        score -= 3.0  # slow intros kill retention
    return round(max(0.0, min(10.0, score)), 1)


def llm_hook_review(cfg: Config, script: scripting.Script) -> tuple[dict[str, Any], float]:
    prompt = f"Hook: {script.hook}\nCaption: {script.caption}\nVoiceover: {script.voiceover}"
    result = complete(cfg.llm, HOOK_SYSTEM, prompt)
    data = scripting.extract_json(result.text)
    return data, result.usd


def virality_gate(
    conn: sqlite3.Connection, cfg: Config, render: sqlite3.Row, use_llm: bool = False
) -> GateResult:
    script = scripting.load(conn, render["script_id"])
    issues: list[str] = []
    info: dict[str, Any] = {}
    if len(script.hook.split()) > 12:
        issues.append("hook over 12 words (must land within 2 s)")
    dur = float(render["duration_s"])
    if not cfg.gates.min_duration_s <= dur <= cfg.gates.max_duration_s:
        issues.append(
            f"duration {dur:.1f}s outside "
            f"{cfg.gates.min_duration_s:.0f}-{cfg.gates.max_duration_s:.0f}s"
        )
    ass = Path(render["path"]).parent / "captions.ass"
    if not ass.exists():
        issues.append("no burned-in caption track found for this render")
    else:
        text = ass.read_text(encoding="utf-8")
        if "Dialogue: 1,0:00:00.00," not in text:
            issues.append("hook text does not appear on screen at 0.0 s")
        if ",Caption," not in text:
            issues.append("no spoken-word captions")
    cta_hits = len(CTA_VERBS.findall(script.cta))
    if cta_hits == 0:
        issues.append("CTA has no clear action (follow/comment/share/save)")
    if len(re.findall(r"[.!?](\s|$)", script.cta.strip())) > 2:
        issues.append("CTA asks for too many things; keep one")
    score = heuristic_hook_score(script.hook)
    info["hook_score_heuristic"] = score
    if use_llm:
        review, usd = llm_hook_review(cfg, script)
        db.record_cost(conn, "llm", usd, "hook review", render["script_id"])
        info["hook_review_llm"] = review
        score = float(review.get("score", score))
    info["hook_score"] = score
    if score < cfg.gates.min_hook_score:
        issues.append(f"hook score {score} below minimum {cfg.gates.min_hook_score}")
    return GateResult("virality", not issues, issues, info)


def run(
    conn: sqlite3.Connection,
    cfg: Config,
    render_id: int,
    use_llm: bool = False,
    today: date | None = None,
) -> GateReport:
    render = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
    if render is None:
        raise ValueError(f"render {render_id} not found")
    report = GateReport(
        render_id,
        [
            facts_gate(conn, render["script_id"]),
            rights_safety_gate(conn, cfg, render["script_id"], today),
            virality_gate(conn, cfg, render, use_llm),
        ],
    )
    conn.execute(
        "INSERT INTO gate_runs (render_id, passed, result_json, created_at) VALUES (?, ?, ?, ?)",
        (render_id, int(report.passed), json.dumps(report.to_dict()), db.now()),
    )
    return report


def latest_passed(conn: sqlite3.Connection, render_id: int) -> bool:
    row = conn.execute(
        "SELECT passed FROM gate_runs WHERE render_id = ? ORDER BY id DESC LIMIT 1", (render_id,)
    ).fetchone()
    return bool(row and row["passed"])
