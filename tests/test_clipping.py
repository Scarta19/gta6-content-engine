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
