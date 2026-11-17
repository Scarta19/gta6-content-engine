from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from gce import analytics, config, db, higgsfield, publish, review, zernio
from tests.test_review_publish import ALL_YES, fake_render


def test_dry_run_makes_no_calls(conn: sqlite3.Connection, cfg: config.Config) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("network used in dry-run")

    client = higgsfield.Client(
        cfg.higgsfield, live=False, http=httpx.Client(transport=httpx.MockTransport(boom))
    )
    out = higgsfield.generate_asset(conn, client, {"prompt": "p"}, Path("x"), None)
    assert out["dry_run"] is True
    assert out["url"] == "https://api.higgsfield.ai/wan/v2.6/image-to-video"


def test_live_generation_downloads_and_registers(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_API_KEY_ID", "id")
    monkeypatch.setenv("HF_API_KEY_SECRET", "sec")
    hcfg = config.HiggsfieldCfg(enabled=True, poll_interval_s=0, est_usd_per_generation=0.5)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            assert req.headers["authorization"] == "Key id:sec"
            assert "idempotency-key" in req.headers
            return httpx.Response(200, json={"status": "queued", "request_id": "r1"})
        if req.url.path == "/requests/r1/status":
            return httpx.Response(
                200, json={"status": "completed", "video": {"url": "https://cdn.hf/out.mp4"}}
            )
        if req.url.host == "cdn.hf":
            return httpx.Response(200, content=b"mp4data")
        return httpx.Response(404)

    client = higgsfield.Client(
        hcfg, live=True, http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    out = higgsfield.generate_asset(conn, client, {"prompt": "neon coast"}, tmp_path, None)
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (out["asset_id"],)).fetchone()
    assert row["license"] == "ai_generated" and row["ai_generated"] == 1
    assert Path(out["path"]).read_bytes() == b"mp4data"
    assert conn.execute("SELECT usd FROM costs").fetchone()[0] == 0.5


def test_live_requires_enabled(cfg: config.Config) -> None:
    client = higgsfield.Client(cfg.higgsfield, live=True)
    with pytest.raises(higgsfield.HiggsfieldError, match="enabled"):
        client.submit({"prompt": "x"})


def test_failed_status_has_no_output() -> None:
    with pytest.raises(higgsfield.HiggsfieldError):
        higgsfield.output_url({"status": "completed"})


def test_metrics_and_report(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = fake_render(conn, tmp_path)
    sid = conn.execute("SELECT script_id FROM renders WHERE id = ?", (rid,)).fetchone()[0]
    db.record_cost(conn, "llm", 0.02, "test", sid)
    db.record_cost(conn, "tts", 0.01, "test", sid)
    review.decide(conn, rid, "Santiago", ALL_YES)
    pub = publish.record_manual(conn, rid, "tiktok", "7300000000000000000")

    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["postId"] == "7300000000000000000"
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "posts": [
                        {
                            "analytics": {
                                "views": 3000,
                                "likes": 200,
                                "comments": 20,
                                "shares": 50,
                                "saves": 30,
                            }
                        }
                    ]
                }
            ),
        )

    zc = zernio.Client(cfg.zernio, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert analytics.sync(conn, cfg, client=zc) == 1
    row = analytics.report(conn, cfg)[0]
    assert row["views"] == 3000 and row["shares"] == 50
    assert row["cost_usd"] == 0.03
    assert row["usd_per_1k_views"] == 0.01
    assert row["eng_rate"] == round(300 / 3000, 4)
    assert pub
