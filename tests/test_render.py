from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from gce import assets, config, media, render, scripting
from tests.conftest import NEWS_SCRIPT

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


def probe(path: Path) -> str:
    return subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_end_to_end_render(conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path) -> None:
    img = tmp_path / "still.png"
    media.run(["-f", "lavfi", "-i", "testsrc2=s=1280x720", "-frames:v", "1", str(img)])
    asset_id = assets.add(conn, img, "owned", source="test pattern")
    sid = scripting.save(conn, scripting.Script.from_dict(NEWS_SCRIPT, "news", None))
    assets.attach(conn, sid, [asset_id])

    res = render.render(conn, cfg, sid)
    assert res.path.exists()
    assert probe(res.path) == "1080,1920"
    assert 15 < media.duration(res.path) < 58
    assert media.has_audio(res.path)
    assert res.srt_path.read_text().startswith("1\n")


def test_render_refuses_failing_script(conn: sqlite3.Connection, cfg: config.Config) -> None:
    bad = {**NEWS_SCRIPT, "claims": [{"claim": "x", "source_url": "u", "status": "unverified"}]}
    sid = scripting.save(conn, scripting.Script.from_dict(bad, "news", None))
    with pytest.raises(ValueError, match="fails checks"):
        render.render(conn, cfg, sid)


def test_unlicensed_asset_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"0")
    with pytest.raises(ValueError, match="license must be"):
        assets.add(conn, f, "found_on_youtube", source="someone")
    with pytest.raises(ValueError, match="provenance"):
        assets.add(conn, f, "permitted", source="creator")


def test_plan_cuts_fast_and_varied() -> None:
    v = [{"id": 1}, {"id": 2}, {"id": 3}]
    slots, seg, offs = render.plan_cuts(v, 30.0, 2.5)  # type: ignore[arg-type]
    assert len(slots) == 12 and abs(seg - 2.5) < 1e-9
    assert [s["id"] for s in slots[:4]] == [1, 2, 3, 1]  # type: ignore[index]
    assert offs[0] == 0 and offs[3] == 2.5  # second use of a clip starts later
    assert render.plan_cuts([], 10.0, 2.5) == ([None], 10.0, [0.0])


def test_render_with_voice_file(
    conn: sqlite3.Connection, cfg: config.Config, tmp_path: Path
) -> None:
    voice = tmp_path / "voice.mp3"
    media.run(["-f", "lavfi", "-i", "sine=f=220:d=22", str(voice)])
    sid = scripting.save(conn, scripting.Script.from_dict(NEWS_SCRIPT, "news", None))
    res = render.render(conn, cfg, sid, voice_file=voice)
    assert 22 < media.duration(res.path) < 23
    assert res.tts_usd == 0.0
