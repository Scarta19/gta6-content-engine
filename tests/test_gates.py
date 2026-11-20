from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from gce import assets, config, gates, media, render, scripting
from tests.conftest import NEWS_SCRIPT


def _script(conn: sqlite3.Connection, fmt: str = "news", **over: object) -> int:
    return scripting.save(conn, scripting.Script.from_dict({**NEWS_SCRIPT, **over}, fmt, None))


def _owned_still(conn: sqlite3.Connection, tmp_path: Path, source: str = "our capture") -> int:
    img = tmp_path / "s.png"
    media.run(["-f", "lavfi", "-i", "color=c=blue:s=320x240", "-frames:v", "1", str(img)])
    return assets.add(conn, img, "owned", source=source)


def test_all_gates_pass_on_clean_render(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    sid = _script(conn)
    assets.attach(conn, sid, [_owned_still(conn, tmp_path)])
    res = render.render(conn, cfg, sid)
    report = gates.run(conn, cfg, res.render_id)
    assert report.passed, report.to_dict()
    assert gates.latest_passed(conn, res.render_id)


def test_kid_directed_wording_blocked(conn: sqlite3.Connection, cfg: config.Config) -> None:
    sid = _script(conn, caption="Fun GTA VI facts for kids! Ask your parents.")
    g = gates.rights_safety_gate(conn, cfg, sid)
    assert not g.passed and "kid-directed" in g.issues[0]


def test_rockstar_asset_blocked(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    sid = _script(conn)
    assets.attach(conn, sid, [_owned_still(conn, tmp_path, source="Rockstar trailer 2 rip")])
    assert not gates.rights_safety_gate(conn, cfg, sid).passed


def test_spoilers_only_blocked_after_launch(conn: sqlite3.Connection, cfg: config.Config) -> None:
    sid = _script(conn, voiceover=NEWS_SCRIPT["voiceover"] + " We explain the ending.")
    assert gates.rights_safety_gate(conn, cfg, sid, today=date(2026, 11, 1)).passed
    assert not gates.rights_safety_gate(conn, cfg, sid, today=date(2026, 12, 1)).passed
    warned = _script(
        conn,
        hook="SPOILER WARNING: the GTA VI ending explained.",
        voiceover=NEWS_SCRIPT["voiceover"] + " We explain the ending.",
    )
    assert gates.rights_safety_gate(conn, cfg, warned, today=date(2026, 12, 1)).passed


def test_hook_heuristic_prefers_tight_curiosity() -> None:
    strong = gates.heuristic_hook_score("Nobody noticed this hidden detail in the trailer")
    weak = gates.heuristic_hook_score("Hey guys welcome back to the channel, today we look at it")
    assert strong > weak
    assert 0 <= weak <= 10 and 0 <= strong <= 10


def test_virality_gate_flags_short_video_and_weak_cta(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    sid = _script(conn, cta="Thanks for watching.")
    work = tmp_path / "r"
    work.mkdir()
    (work / "captions.ass").write_text(
        "Dialogue: 1,0:00:00.00,0:00:02.00,Hook,,0,0,0,,x\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Caption,,0,0,0,,y\n"
    )
    conn.execute(
        "INSERT INTO renders (script_id, path, srt_path, duration_s, created_at)"
        " VALUES (?, ?, '', 12, 'now')",
        (sid, str(work / "v.mp4")),
    )
    row = conn.execute("SELECT * FROM renders ORDER BY id DESC LIMIT 1").fetchone()
    g = gates.virality_gate(conn, cfg, row)
    assert not g.passed
    assert any("duration" in i for i in g.issues)
    assert any("CTA" in i for i in g.issues)


def test_caption_ai_disclosure_is_optional(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    import dataclasses

    from gce import assets as assets_mod
    from gce import gates as gates_mod

    sid = scripting.save(conn, scripting.Script.from_dict(dict(NEWS_SCRIPT), "news", None))
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00")
    assets_mod.attach(conn, sid, [assets_mod.add(conn, clip, "ai_generated", "higgsfield")])
    off = gates_mod.rights_safety_gate(conn, cfg, sid)
    assert not any("AI disclosure" in i for i in off.issues)
    strict = dataclasses.replace(
        cfg, gates=dataclasses.replace(cfg.gates, require_caption_ai_disclosure=True)
    )
    on = gates_mod.rights_safety_gate(conn, strict, sid)
    assert any("AI disclosure" in i for i in on.issues)
