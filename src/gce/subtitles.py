"""Caption timing and ASS/SRT generation. Deterministic: no speech recognition needed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gce.config import RenderCfg


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def chunk_words(text: str, max_words: int) -> list[str]:
    """Split into short caption chunks, breaking early at sentence punctuation."""
    chunks: list[str] = []
    current: list[str] = []
    for word in text.split():
        current.append(word)
        if len(current) >= max_words or re.search(r"[.!?,;:]$", word):
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def time_cues(text: str, total_seconds: float, max_words: int) -> list[Cue]:
    """Distribute duration proportional to characters (a good proxy for speech time)."""
    chunks = chunk_words(text, max_words)
    weights = [len(c) + 3 for c in chunks]  # +3 approximates inter-word pauses
    total_w = sum(weights) or 1
    cues: list[Cue] = []
    t = 0.0
    for chunk, w in zip(chunks, weights, strict=True):
        dur = total_seconds * w / total_w
        cues.append(Cue(round(t, 3), round(t + dur, 3), chunk))
        t += dur
    return cues


def _srt_ts(s: float) -> str:
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def _ass_ts(s: float) -> str:
    cs = int(round(s * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{sec:02}.{cs:02}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def write_srt(cues: list[Cue], path: Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_srt_ts(c.start)} --> {_srt_ts(c.end)}", c.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass(
    cues: list[Cue],
    path: Path,
    cfg: RenderCfg,
    hook: str,
    beats: list[str],
    total_seconds: float,
    ai_label: bool,
) -> Path:
    w, h = cfg.width, cfg.height
    # Captions sit in the lower-middle safe zone, clear of TikTok/Reels UI at the bottom.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{cfg.font},{cfg.font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,620,1
Style: Hook,{cfg.font},{int(cfg.font_size * 1.25)},&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,8,70,70,330,1
Style: Beat,{cfg.font},{int(cfg.font_size * 0.95)},&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,4,0,8,90,90,330,1
Style: Label,{cfg.font},{int(cfg.font_size * 0.5)},&H00FFFFFF,&H000000FF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,3,0,8,40,40,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501
    events: list[str] = []
    hook_end = min(2.8, total_seconds)
    events.append(f"Dialogue: 1,{_ass_ts(0)},{_ass_ts(hook_end)},Hook,,0,0,0,,{_ass_escape(hook)}")
    if beats:
        span = (total_seconds - hook_end) / len(beats)
        for i, beat in enumerate(beats):
            start = hook_end + i * span
            events.append(
                f"Dialogue: 1,{_ass_ts(start)},{_ass_ts(start + span)},Beat,,0,0,0,,"
                f"{_ass_escape(beat)}"
            )
    if ai_label:
        events.append(
            f"Dialogue: 2,{_ass_ts(0)},{_ass_ts(total_seconds)},Label,,0,0,0,,"
            f"{_ass_escape(cfg.ai_label_text)}"
        )
    for c in cues:
        events.append(
            f"Dialogue: 0,{_ass_ts(c.start)},{_ass_ts(c.end)},Caption,,0,0,0,,{_ass_escape(c.text)}"
        )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
