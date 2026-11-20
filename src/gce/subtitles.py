"""Caption timing and ASS/SRT generation. Deterministic: no speech recognition needed.

With real narration audio, sentences are snapped to the detected pauses so captions and
numbered beat cards follow the voice instead of an even split."""

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


# Motion in the first frames: text scales in instead of appearing flat.
POP_IN = r"{\fscx60\fscy60\t(0,180,\fscx108\fscy108)\t(180,260,\fscx100\fscy100)}"
CAPTION_POP = r"{\fscx88\fscy88\t(0,90,\fscx100\fscy100)}"


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


SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_RE.split(text.strip()) if s]


def _offset(cues: list[Cue], t0: float) -> list[Cue]:
    return [Cue(round(c.start + t0, 3), round(c.end + t0, 3), c.text) for c in cues]


def sentence_spans(
    text: str, total_seconds: float, pauses: list[tuple[float, float]]
) -> list[tuple[str, float, float]]:
    """(sentence, start, end) using the pause nearest each sentence's expected boundary."""
    sents = sentences(text)
    if len(sents) <= 1 or not pauses:
        weights = [len(s) + 3 for s in sents] or [1]
        tw = sum(weights)
        spans, t = [], 0.0
        for s, w in zip(sents, weights, strict=True):
            d = total_seconds * w / tw
            spans.append((s, t, t + d))
            t += d
        return spans
    inner = [p for p in pauses if p[0] > 0.3 and p[1] < total_seconds - 0.2]
    weights = [len(s) + 3 for s in sents]
    tw = sum(weights)
    bounds: list[tuple[float, float]] = []
    acc = 0.0
    used = -1
    for w in weights[:-1]:
        acc += total_seconds * w / tw
        # Nearest unused pause after the previous one; fall back to the estimate.
        cands = [(abs((p[0] + p[1]) / 2 - acc), i) for i, p in enumerate(inner) if i > used]
        if cands:
            dist, i = min(cands)
            if dist < 2.5:
                used = i
                bounds.append(inner[i])
                continue
        bounds.append((acc, acc))
    spans = []
    start = 0.0
    for s, (b0, b1) in zip(sents, [*bounds, (total_seconds, total_seconds)], strict=True):
        spans.append((s, start, max(b0, start + 0.2)))
        start = b1
    return spans


def align_cues(
    text: str, total_seconds: float, max_words: int, pauses: list[tuple[float, float]]
) -> list[Cue]:
    cues: list[Cue] = []
    for sent, t0, t1 in sentence_spans(text, total_seconds, pauses):
        cues += _offset(time_cues(sent, t1 - t0, max_words), t0)
    return cues


NUMBER_WORDS = {
    1: ("one", "uno", "um", "un", "1"),
    2: ("two", "dos", "dois", "deux", "2"),
    3: ("three", "tres", "três", "trois", "3"),
    4: ("four", "cuatro", "quatro", "quatre", "4"),
    5: ("five", "cinco", "cinq", "5"),
    6: ("six", "seis", "6"),
    7: ("seven", "siete", "sete", "sept", "7"),
}


def beat_starts(beats: list[str], cues: list[Cue]) -> list[float] | None:
    """Start time of each numbered beat card ("1. ...") at the cue that says that number."""
    out: list[float] = []
    for beat in beats:
        m = re.match(r"\s*#?(\d+)[.):]", beat)
        if not m or int(m.group(1)) not in NUMBER_WORDS:
            out.append(out[-1] if out else 0.0)  # title card: show right after the hook
            continue
        words = NUMBER_WORDS[int(m.group(1))]
        after = out[-1] if out else 0.0
        hit = next(
            (
                c.start
                for c in cues
                if c.start >= after
                and re.sub(r"[^\w]", "", c.text.split()[0].lower()) in words
                and re.search(r"[:.,]", c.text.split()[0] + " ")
            ),
            None,
        )
        if hit is None:
            return None
        out.append(hit)
    return out


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
    starts: list[float] | None = None,
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
Style: Brand,{cfg.font},{int(cfg.font_size * 0.45)},&H50FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,3,40,150,60,1
Style: Label,{cfg.font},{int(cfg.font_size * 0.62)},&H00FFFFFF,&H000000FF,&H00000000,&H200000D0,-1,0,0,0,100,100,0,0,3,3,0,8,40,40,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501
    events: list[str] = []
    hook_end = min(2.8, total_seconds)
    events.append(
        f"Dialogue: 1,{_ass_ts(0)},{_ass_ts(hook_end)},Hook,,0,0,0,,{POP_IN}{_ass_escape(hook)}"
    )
    if beats:
        if starts is None or len(starts) != len(beats):
            span = (total_seconds - hook_end) / len(beats)
            starts = [hook_end + i * span for i in range(len(beats))]
        else:
            starts = [max(s, hook_end) for s in starts]
        ends = [*starts[1:], total_seconds]
        for beat, start, end in zip(beats, starts, ends, strict=True):
            events.append(
                f"Dialogue: 1,{_ass_ts(start)},{_ass_ts(end)},Beat,,0,0,0,,"
                f"{POP_IN}{_ass_escape(beat)}"
            )
    if ai_label:
        events.append(
            f"Dialogue: 2,{_ass_ts(0)},{_ass_ts(total_seconds)},Label,,0,0,0,,"
            f"{_ass_escape(cfg.ai_label_text)}"
        )
    if cfg.watermark:
        events.append(
            f"Dialogue: 2,{_ass_ts(0)},{_ass_ts(total_seconds)},Brand,,0,0,0,,"
            f"{_ass_escape(cfg.watermark)}"
        )
    for c in cues:
        events.append(
            f"Dialogue: 0,{_ass_ts(c.start)},{_ass_ts(c.end)},Caption,,0,0,0,,"
            f"{CAPTION_POP}{_ass_escape(c.text)}"
        )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
