from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from gce import config, db, publish, review, scripting, zernio
from tests.conftest import NEWS_SCRIPT

ALL_YES = {k: True for k, _ in review.CHECKLIST}


def fake_render(conn: sqlite3.Connection, tmp_path: Path, fmt: str = "news") -> int:
    data = dict(NEWS_SCRIPT)
    if fmt == "concept":
        data |= {
            "voiceover": "This is an AI-generated concept. " + data["voiceover"],
            "caption": "AI-generated fan concept.",
            "claims": [],
        }
    sid = scripting.save(conn, scripting.Script.from_dict(data, fmt, None))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 16)
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    cur = conn.execute(
        "INSERT INTO renders (script_id, path, srt_path, duration_s, created_at)"
        " VALUES (?, ?, ?, 40, ?)",
        (sid, str(video), str(srt), db.now()),
    )
    assert cur.lastrowid
    conn.execute(
        "INSERT INTO gate_runs (render_id, passed, result_json, created_at) VALUES (?, 1, '{}', ?)",
        (cur.lastrowid, db.now()),
    )
    return cur.lastrowid


def test_review_requires_full_checklist(conn: sqlite3.Connection, tmp_path: Path) -> None:
    rid = fake_render(conn, tmp_path)
    with pytest.raises(ValueError, match="incomplete"):
        review.decide(conn, rid, "Santiago", {"watched": True})
    assert review.decide(conn, rid, "Santiago", {**ALL_YES, "rights": False}) == "rejected"
    assert not review.is_approved(conn, rid)
    assert review.decide(conn, rid, "Santiago", ALL_YES) == "approved"
    assert review.is_approved(conn, rid)


def test_review_blocked_when_gates_fail(conn: sqlite3.Connection, tmp_path: Path) -> None:
    rid = fake_render(conn, tmp_path)
    conn.execute(
        "INSERT INTO gate_runs (render_id, passed, result_json, created_at) VALUES (?, 0, '{}', ?)",
        (rid, db.now()),
    )
    assert review.decide(conn, rid, "Santiago", ALL_YES) == "rejected"


def test_dry_run_publish_sends_nothing(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    rid = fake_render(conn, tmp_path)
    out = publish.publish(conn, cfg, rid, ["tiktok", "instagram"])
    assert out["mode"] == "dry_run"
    body = out["request"]
    assert body["publishNow"] is True
    assert body["tiktokSettings"]["express_consent_given"] is True
    assert "#gta6" in body["content"]
    modes = {r["mode"] for r in conn.execute("SELECT mode FROM publications")}
    assert modes == {"dry_run"}


def test_live_publish_gates(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_render(conn, tmp_path)
    with pytest.raises(PermissionError, match="not approved"):
        publish.publish(conn, cfg, rid, ["tiktok"], live=True)
    review.decide(conn, rid, "Santiago", ALL_YES)
    monkeypatch.delenv("GCE_ALLOW_LIVE_PUBLISH", raising=False)
    with pytest.raises(PermissionError, match="GCE_ALLOW_LIVE_PUBLISH"):
        publish.publish(conn, cfg, rid, ["tiktok"], live=True)


def test_concept_needs_ai_disclosure_ack(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_render(conn, tmp_path, "concept")
    review.decide(conn, rid, "Santiago", ALL_YES)
    monkeypatch.setenv("GCE_ALLOW_LIVE_PUBLISH", "yes")
    with pytest.raises(PermissionError, match="ai-disclosure"):
        publish.publish(conn, cfg, rid, ["tiktok"], live=True)


def test_live_publish_with_mock_zernio(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_render(conn, tmp_path)
    review.decide(conn, rid, "Santiago", ALL_YES)
    monkeypatch.setenv("GCE_ALLOW_LIVE_PUBLISH", "yes")
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/media/presign"):
            assert json.loads(req.content)["contentType"] == "video/mp4"
            return httpx.Response(
                200,
                json={
                    "uploadUrl": "https://storage.test/up",
                    "publicUrl": "https://cdn.test/v.mp4",
                },
            )
        if req.url.host == "storage.test":
            assert "authorization" not in req.headers
            return httpx.Response(200)
        if req.url.path.endswith("/posts"):
            body = json.loads(req.content)
            assert body["mediaItems"] == [{"url": "https://cdn.test/v.mp4", "type": "video"}]
            assert body["platforms"][0]["accountId"] == "a" * 24
            return httpx.Response(200, json={"post": {"_id": "p" * 24, "status": "publishing"}})
        return httpx.Response(404)

    client = zernio.Client(cfg.zernio, http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = publish.publish(conn, cfg, rid, ["tiktok"], live=True, client=client)
    assert out == {"mode": "live", "post_id": "p" * 24, "status": "publishing"}
    assert [c[0] for c in calls] == ["POST", "PUT", "POST"]


def test_missing_account_id_is_an_error() -> None:
    with pytest.raises(zernio.ZernioError, match="instagram"):
        zernio.build_post_body(
            caption="c",
            media_url="u",
            platforms=["instagram"],
            accounts={},
            timezone="America/New_York",
            scheduled_for=None,
            tiktok_privacy="PUBLIC_TO_EVERYONE",
        )


def test_scheduled_post_body() -> None:
    body = zernio.build_post_body(
        caption="c",
        media_url="u",
        platforms=["facebook"],
        accounts={"facebook": "f"},
        timezone="America/New_York",
        scheduled_for="2026-11-20T18:00:00",
        tiktok_privacy="PUBLIC_TO_EVERYONE",
    )
    assert "publishNow" not in body and body["timezone"] == "America/New_York"
    assert "tiktokSettings" not in body


def test_export_package(conn: sqlite3.Connection, tmp_path: Path) -> None:
    rid = fake_render(conn, tmp_path)
    folder = publish.export(conn, rid, tmp_path / "exp")
    assert {p.name for p in folder.iterdir()} == {
        "video.mp4",
        "captions.srt",
        "caption.txt",
        "review.json",
    }
