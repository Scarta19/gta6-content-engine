from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gce import assets, config, db, gates, publish, scripting, zernio
from tests.conftest import NEWS_SCRIPT
from tests.test_review_publish import fake_render

GUIDE = {
    **NEWS_SCRIPT,
    "title": "Cómo conseguir el coche más rápido",
    "hook": "Este coche secreto casi nadie lo encuentra.",
    "cta": "Guarda este video y síguenos para más guías.",
    "caption": "Guía rápida: dónde está el coche más rápido.",
    "claims": [
        {
            "claim": "El coche aparece en el puerto",
            "source_url": "https://ign.com/g",
            "status": "corroborated",
        }
    ],
}


def test_all_platforms_body_has_platform_specific_data() -> None:
    body = zernio.build_post_body(
        caption="c",
        media_url="u",
        platforms=list(publish.PLATFORMS),
        accounts={p: p[0] * 24 for p in publish.PLATFORMS},
        timezone="America/New_York",
        scheduled_for=None,
        tiktok_privacy="PUBLIC_TO_EVERYONE",
        title="T" * 150,
        tags=["#gta6", "gtavi"],
        ai_generated=True,
    )
    by = {p["platform"]: p.get("platformSpecificData") for p in body["platforms"]}
    yt = by["youtube"]
    assert yt is not None
    assert yt["madeForKids"] is False and yt["containsSyntheticMedia"] is True
    assert yt["categoryId"] == "20" and len(yt["title"]) == 100
    assert by["facebook"] == {"contentType": "reel", "title": "T" * 150}
    assert by["instagram"] == {"shareToFeed": True}
    assert by["tiktok"] is None
    assert body["tags"] == ["gta6", "gtavi"]


def test_dry_run_includes_youtube(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    rid = fake_render(conn, tmp_path)
    out = publish.publish(conn, cfg, rid, ["youtube"])
    assert out["request"]["platforms"][0]["platform"] == "youtube"


def test_spanish_guide_passes_and_roundtrips(conn: sqlite3.Connection) -> None:
    sid = scripting.save(conn, scripting.Script.from_dict(GUIDE, "guide", None, "es"))
    s = scripting.load(conn, sid)
    assert s.language == "es"
    assert scripting.check(conn, sid) == []


def test_niche_formats_need_sourced_claims() -> None:
    for fmt in ("curiosity", "guide", "mystery"):
        s = scripting.Script.from_dict({**GUIDE, "claims": []}, fmt, None, "es")
        assert any("claims" in p for p in scripting.validate(s, None, set()))


def test_mystery_theory_must_be_labeled() -> None:
    claim = [{"claim": "x", "source_url": "https://reddit.com/x", "status": "unverified"}]
    s = scripting.Script.from_dict({**NEWS_SCRIPT, "claims": claim}, "mystery", None)
    assert any("rumor/theory" in p for p in scripting.validate(s, None, set()))
    s.voiceover += " This is only a fan theory."
    assert not any("rumor/theory" in p for p in scripting.validate(s, None, set()))


def test_unsupported_language() -> None:
    s = scripting.Script.from_dict(GUIDE, "guide", None, "de")
    assert any("language" in p for p in scripting.validate(s, None, set()))


@pytest.mark.parametrize(
    "text", ["Trucos para niños", "Dicas para crianças", "Astuces pour les enfants"]
)
def test_kid_wording_blocked_in_other_languages(
    conn: sqlite3.Connection, cfg: config.Config, text: str
) -> None:
    sid = scripting.save(
        conn, scripting.Script.from_dict({**GUIDE, "caption": text}, "guide", None, "es")
    )
    assert not gates.rights_safety_gate(conn, cfg, sid).passed


def test_spanish_cta_recognized() -> None:
    assert gates.CTA_VERBS.search("Guarda este video y síguenos")
    assert gates.CTA_VERBS.search("Compartilhe com um amigo")


def test_asset_from_inspiration_source_blocked(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    sid = scripting.save(conn, scripting.Script.from_dict(GUIDE, "guide", None, "es"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0")
    aid = assets.add(conn, clip, "owned", source="downloaded from CtrlAltPlay")
    assets.attach(conn, sid, [aid])
    conn.execute(
        "INSERT INTO inspirations (script_id, url, creator, platform, note, created_at)"
        " VALUES (?, 'https://youtube.com/@x', 'CtrlAltPlay', 'youtube', 'format', ?)",
        (sid, db.now()),
    )
    g = gates.rights_safety_gate(conn, cfg, sid)
    assert not g.passed and any("inspiration" in i for i in g.issues)


def test_migration_adds_language_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE scripts (id INTEGER PRIMARY KEY, format TEXT)")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scripts)")}
    assert "language" in cols


def test_plural_ai_disclosure_in_spanish_and_portuguese() -> None:
    assert scripting.AI_DISCLOSURE_RE.search("Visuales generados con IA")
    assert scripting.AI_DISCLOSURE_RE.search("Imagens geradas por IA")
