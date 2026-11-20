from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gce import clipping, config, media


def _source(conn: sqlite3.Connection, tmp_path: Path) -> tuple[int, int]:
    video = tmp_path / "vod.mp4"
    media.run(
        [
            "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=40",
            "-f", "lavfi", "-i", "sine=f=330:d=40",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(video),
        ]
    )  # fmt: skip
    cid = clipping.add_campaign(
        conn, "Streamer GTA", 1.5, min_payout=3, max_payout=300, required_text="#ad @streamer"
    )
    sid = clipping.add_source(conn, cid, video, "https://campaign.example/vod", authorized=True)
    words = [
        {"start": 1.0 + i * 0.5, "end": 1.4 + i * 0.5, "text": w}
        for i, w in enumerate(("No", "way", "he", "jumped", "the", "bridge!") * 10)
    ]
    clipping.save_transcript(conn, sid, words)
    return cid, sid


def test_unauthorized_source_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cid = clipping.add_campaign(conn, "X", 1.0)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"0")
    with pytest.raises(PermissionError):
        clipping.add_source(conn, cid, f, "https://x", authorized=False)


def test_payout_rules() -> None:
    assert clipping.payout(1500, 1.5, 3, 300) == 0.0  # $2.25 < $3 minimum
    assert clipping.payout(10_000, 1.5, 3, 300) == 15.0
    assert clipping.payout(1_000_000, 1.5, 3, 300) == 300.0


def test_clip_render_post_report(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    _, sid = _source(conn, tmp_path)
    words = clipping.words_of(conn, sid)
    s, e = clipping.snap_to_words(words, 3.1, 25.0)
    clip_id = clipping.save_clip(conn, sid, s, e, hook="He jumped the bridge?!", title="Insane")
    png = tmp_path / "overlay.png"
    media.run(
        ["-f", "lavfi", "-i", "color=c=red@0.5:s=1080x200,format=rgba", "-frames:v", "1", str(png)]
    )
    clipping.set_overlay(conn, 1, png)
    out = clipping.render_clip(conn, cfg, clip_id)
    assert out.exists() and media.has_audio(out)
    assert abs(media.duration(out) - (e - s)) < 0.3
    assert "#ad @streamer" in (out.parent / "caption.txt").read_text()
    with pytest.raises(PermissionError):
        clipping.record_post(conn, clip_id, "tiktok", "https://tiktok.com/@x/video/1")
    clipping.decide(conn, clip_id, "Santiago", approve=True)
    with pytest.raises(ValueError):
        clipping.record_post(conn, clip_id, "facebook", "https://fb.com/1")
    pid = clipping.record_post(conn, clip_id, "tiktok", "https://tiktok.com/@x/video/1")
    clipping.set_views(conn, pid, 40_000, submitted=True)
    rep = clipping.report(conn)[0]
    assert rep["views"] == 40_000 and rep["est_gross_usd"] == 60.0 and rep["est_net_usd"] == 55.8


def test_clip_length_bounds(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _, sid = _source(conn, tmp_path)
    with pytest.raises(ValueError):
        clipping.save_clip(conn, sid, 0, 3)
    with pytest.raises(ValueError):
        clipping.save_clip(conn, sid, 10, 60)  # past the 40 s source


def test_pool_campaign_counts_only_qualifying_views(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cid = clipping.add_campaign(conn, "Pool", 0, pool_usd=5000, min_views=400_000)
    video = tmp_path / "v.mp4"
    media.run(["-f", "lavfi", "-i", "testsrc2=s=640x360:d=30", "-c:v", "libx264", str(video)])
    sid = clipping.add_source(conn, cid, video, "https://twitch.tv/videos/1", authorized=True)
    clip_id = clipping.save_clip(conn, sid, 0, 20)
    conn.execute("UPDATE clips SET status = 'approved' WHERE id = ?", (clip_id,))
    a = clipping.record_post(conn, clip_id, "tiktok", "https://t/1")
    b = clipping.record_post(conn, clip_id, "youtube", "https://y/1")
    clipping.set_views(conn, a, 500_000, submitted=True)
    clipping.set_views(conn, b, 90_000, submitted=True)
    rep = clipping.report(conn)[0]
    assert rep["kind"] == "pool" and rep["qualifying_posts"] == 1
    assert rep["qualifying_views"] == 500_000 and rep["est_gross_usd"] == 0.0


def test_publish_clip_via_zernio(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import httpx

    from gce import zernio

    _, sid = _source(conn, tmp_path)
    clip_id = clipping.save_clip(conn, sid, 3.0, 15.0, hook="He jumped?!", title="Insane jump")
    clipping.render_clip(conn, cfg, clip_id)
    dry = clipping.publish_clip(conn, cfg, clip_id, ["tiktok", "youtube"])
    assert dry["mode"] == "dry_run" and "#ad @streamer" in dry["request"]["content"]
    with pytest.raises(ValueError):
        clipping.publish_clip(conn, cfg, clip_id, ["facebook"])
    with pytest.raises(PermissionError):
        clipping.publish_clip(conn, cfg, clip_id, ["tiktok"], live=True)
    clipping.decide(conn, clip_id, "Santiago", approve=True)
    with pytest.raises(PermissionError):
        clipping.publish_clip(conn, cfg, clip_id, ["tiktok"], live=True)
    monkeypatch.setenv("GCE_ALLOW_LIVE_PUBLISH", "yes")
    monkeypatch.setenv("ZERNIO_API_KEY", "sk_test")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/media/presign"):
            return httpx.Response(
                200, json={"uploadUrl": "https://storage.test/up", "publicUrl": "https://cdn/v"}
            )
        if req.url.host == "storage.test":
            return httpx.Response(200)
        if req.url.path.endswith("/posts"):
            body = json.loads(req.content)
            assert body["platforms"][0]["accountId"] == "a" * 24
            return httpx.Response(200, json={"post": {"_id": "p" * 24}})
        return httpx.Response(404)

    client = zernio.Client(cfg.zernio, http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = clipping.publish_clip(conn, cfg, clip_id, ["tiktok"], live=True, client=client)
    assert out["mode"] == "live" and len(out["clip_posts"]) == 1
    pending = clipping.unsubmitted(conn)
    assert [r["url"] for r in pending] == ["zernio:" + "p" * 24]


def test_clip_thumbnail(conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path) -> None:
    _, sid = _source(conn, tmp_path)
    clip_id = clipping.save_clip(conn, sid, 3.0, 15.0, hook="He jumped?!", title="Insane jump")
    png = tmp_path / "overlay.png"
    media.run(
        ["-f", "lavfi", "-i", "color=c=red@0.5:s=1080x200,format=rgba", "-frames:v", "1", str(png)]
    )
    clipping.set_overlay(conn, 1, png)
    out = clipping.thumbnail(conn, cfg, clip_id, "He jumped the bridge 😭", at=4.0)
    assert out.name == "cover.jpg" and out.stat().st_size > 10_000
    with pytest.raises(ValueError):
        clipping.thumbnail(conn, cfg, clip_id, "x", at=99.0)
    faced = clipping.thumbnail(conn, cfg, clip_id, "Reaction", at=4.0, face=(0, 0, 0.3, 0.35))
    assert faced.stat().st_size > 10_000
    with pytest.raises(ValueError):
        clipping.thumbnail(conn, cfg, clip_id, "x", face=(0.9, 0, 0.3, 0.3))


def test_campaign_credit_drawn_in_logo_slot(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    _, sid = _source(conn, tmp_path)
    conn.execute("UPDATE campaigns SET watermark = 'streamer on Kick' WHERE id = 1")
    clip_id = clipping.save_clip(conn, sid, 3.0, 12.0, hook="No way", title="t")
    out = clipping.render_clip(conn, cfg, clip_id)
    credit = (out.parent / "credit.ass").read_text()
    assert "streamer on Kick" in credit
    assert "streamer on Kick" not in (out.parent / "captions.ass").read_text()
