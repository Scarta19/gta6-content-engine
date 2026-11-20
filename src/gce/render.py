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


def _segment(
    asset: sqlite3.Row | None, seconds: float, out: Path, cfg: Config, offset: float = 0.0
) -> Path:
    r = cfg.render
    w, h, fps = r.width, r.height, r.fps
    common = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(fps)]
    if asset is None:
        media.run(
            [
                "-f",
                "lavfi",
                "-i",
                # Animated, non-repeating gradient so the frame is never static.
                f"gradients=s={w}x{h}:r={fps}:c0=0x1b1464:c1=0xff3d8b:c2=0x00c2ff:"
                f"nb_colors=3:speed=0.008:d={seconds:.3f}",
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
        clip_len = media.duration(Path(asset["path"]))
        offset = offset % clip_len if clip_len > seconds else 0.0
        media.run(
            [
                "-stream_loop",
                "-1",
                "-ss",
                f"{offset:.3f}",
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


def plan_cuts(
    visuals: list[sqlite3.Row], total: float, scene_seconds: float
) -> tuple[list[sqlite3.Row | None], float, list[float]]:
    """Fast-cut plan: ~scene_seconds per shot, cycling visuals, each reuse starting later
    in the clip so repeated sources show different moments."""
    if not visuals:
        return [None], total, [0.0]
    n = max(len(visuals), round(total / max(scene_seconds, 0.5)))
    seg_len = total / n
    slots: list[sqlite3.Row | None] = [visuals[i % len(visuals)] for i in range(n)]
    offsets = [(i // len(visuals)) * seg_len for i in range(n)]
    return slots, seg_len, offsets


def render(
    conn: sqlite3.Connection,
    cfg: Config,
    script_id: int,
    force: bool = False,
    voice_file: Path | None = None,
) -> RenderResult:
    problems = scripting.check(conn, script_id)
    if problems and not force:
        raise ValueError(
            "script fails checks (use --force to render a draft anyway):\n- "
            + "\n- ".join(problems)
        )
    media.require_libass()
    script = scripting.load(conn, script_id)
    visuals = [a for a in assets.for_script(conn, script_id) if a["kind"] in ("image", "video")]
    work = cfg.paths.work_dir / f"script_{script_id}"
    work.mkdir(parents=True, exist_ok=True)

    narration = f"{script.voiceover} {script.cta}".strip()
    if voice_file is not None:
        # Pre-generated narration (e.g. a natural Higgsfield/ElevenLabs voice). It must read the
        # script's voiceover + CTA; captions are timed against its length.
        audio_path, tts_usd = work / "voice.wav", 0.0
        media.run(["-i", str(voice_file), "-ac", "1", "-ar", "48000", str(audio_path)])
    else:
        audio_path, tts_usd = tts.synthesize(narration, work / "voice", cfg.tts, script.language)
    total = media.duration(audio_path) + 0.4  # small tail so the last caption can be read
    if total > cfg.render.max_duration_s and not force:
        raise ValueError(
            f"voiceover runs {total:.1f}s, over the {cfg.render.max_duration_s}s cap "
            "(Facebook Reels max 60s). Shorten the script."
        )

    cues = subtitles.time_cues(narration, total - 0.4, cfg.render.max_words_per_caption)
    srt = subtitles.write_srt(cues, work / "captions.srt")
    ai_label = cfg.render.show_ai_label and (
        script.ai_generated or any(a["ai_generated"] for a in visuals)
    )
    subtitles.write_ass(
        cues, work / "captions.ass", cfg.render, script.hook, script.on_screen, total, ai_label
    )

    slots, seg_len, offsets = plan_cuts(list(visuals), total, cfg.render.scene_seconds)
    seg_paths = [
        _segment(a, seg_len, work / f"seg_{i:02}.mp4", cfg, off)
        for i, (a, off) in enumerate(zip(slots, offsets, strict=True))
    ]
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
            # Captions, then a thin progress bar (a proven retention cue on Shorts/Reels).
            f"ass=captions.ass,drawbox=x=0:y=ih-14:w=iw*t/{total:.3f}:h=14:"
            "color=0xFFE500@0.95:t=fill",
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
