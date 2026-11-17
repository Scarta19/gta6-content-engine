"""9:16 video rendering with FFmpeg: background segments + voiceover + burned-in captions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from gce import assets, db, media, scripting, subtitles, tts
from gce.config import Config


@dataclass(frozen=True)
class RenderResult:
    render_id: int
    path: Path
    srt_path: Path
    duration_s: float
    tts_usd: float


def _cover_filter(w: int, h: int, fps: int) -> str:
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"fps={fps},setsar=1,format=yuv420p"
    )


def _segment(asset: sqlite3.Row | None, seconds: float, out: Path, cfg: Config) -> Path:
    r = cfg.render
    w, h, fps = r.width, r.height, r.fps
    common = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(fps)]
    if asset is None:
        media.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={r.background_color}:s={w}x{h}:r={fps}",
                "-t",
                f"{seconds:.3f}",
                *common,
                str(out),
            ]
        )
    elif asset["kind"] == "image":
        frames = max(1, int(seconds * fps))
        # Slow Ken Burns push-in; upscale first so zoompan stays smooth.
        vf = (
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
            f"zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={w}x{h}:fps={fps},setsar=1,format=yuv420p"
        )
        media.run(
            [
                "-loop",
                "1",
                "-i",
                asset["path"],
                "-vf",
                vf,
                "-t",
                f"{seconds:.3f}",
                *common,
                str(out),
            ]
        )
    elif asset["kind"] == "video":
        media.run(
            [
                "-stream_loop",
                "-1",
                "-i",
                asset["path"],
                "-vf",
                _cover_filter(w, h, fps),
                "-t",
                f"{seconds:.3f}",
                *common,
                str(out),
            ]
        )
    else:
        raise ValueError(f"asset {asset['id']} of kind {asset['kind']} cannot be a visual")
    return out


def render(
    conn: sqlite3.Connection, cfg: Config, script_id: int, force: bool = False
) -> RenderResult:
    problems = scripting.check(conn, script_id)
    if problems and not force:
        raise ValueError(
            "script fails checks (use --force to render a draft anyway):\n- "
            + "\n- ".join(problems)
        )
    script = scripting.load(conn, script_id)
    visuals = [a for a in assets.for_script(conn, script_id) if a["kind"] in ("image", "video")]
    work = cfg.paths.work_dir / f"script_{script_id}"
    work.mkdir(parents=True, exist_ok=True)

    narration = f"{script.voiceover} {script.cta}".strip()
    audio_path, tts_usd = tts.synthesize(narration, work / "voice", cfg.tts)
    total = media.duration(audio_path) + 0.4  # small tail so the last caption can be read
    if total > cfg.render.max_duration_s and not force:
        raise ValueError(
            f"voiceover runs {total:.1f}s, over the {cfg.render.max_duration_s}s cap "
            "(Facebook Reels max 60s). Shorten the script."
        )

    cues = subtitles.time_cues(narration, total - 0.4, cfg.render.max_words_per_caption)
    srt = subtitles.write_srt(cues, work / "captions.srt")
    ai_label = script.ai_generated or any(a["ai_generated"] for a in visuals)
    subtitles.write_ass(
        cues, work / "captions.ass", cfg.render, script.hook, script.on_screen, total, ai_label
    )

    slots: list[sqlite3.Row | None] = list(visuals) or [None]
    seg_len = total / len(slots)
    seg_paths = [_segment(a, seg_len, work / f"seg_{i:02}.mp4", cfg) for i, a in enumerate(slots)]
    concat_list = work / "segments.txt"
    concat_list.write_text("".join(f"file '{p.name}'\n" for p in seg_paths), encoding="utf-8")
    media.run(
        ["-f", "concat", "-safe", "0", "-i", concat_list.name, "-c", "copy", "bg.mp4"], cwd=work
    )

    out_name = f"gce_{script_id}.mp4"
    media.run(
        [
            "-i",
            "bg.mp4",
            "-i",
            audio_path.name,
            "-vf",
            "ass=captions.ass",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-t",
            f"{total:.3f}",
            out_name,
        ],
        cwd=work,
    )
    out = work / out_name
    if tts_usd:
        db.record_cost(conn, "tts", tts_usd, cfg.tts.provider, script_id)
    cur = conn.execute(
        "INSERT INTO renders (script_id, path, srt_path, duration_s, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            script_id,
            str(out.resolve()),
            str(srt.resolve()),
            round(media.duration(out), 2),
            db.now(),
        ),
    )
    assert cur.lastrowid is not None
    return RenderResult(cur.lastrowid, out, srt, total, tts_usd)
