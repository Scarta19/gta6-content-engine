from __future__ import annotations

from pathlib import Path

from gce import config, subtitles


def test_chunks_respect_limit_and_punctuation() -> None:
    chunks = subtitles.chunk_words("One two three four five. Six seven", 4)
    assert chunks == ["One two three four", "five.", "Six seven"]


def test_cues_cover_duration() -> None:
    cues = subtitles.time_cues("a b c d e f g h i j", 10.0, 3)
    assert cues[0].start == 0
    assert abs(cues[-1].end - 10.0) < 0.01
    assert all(c.end > c.start for c in cues)


def test_ass_includes_ai_label(tmp_path: Path, cfg: config.Config) -> None:
    cues = subtitles.time_cues("hello world", 3.0, 4)
    path = subtitles.write_ass(cues, tmp_path / "c.ass", cfg.render, "Hook {x}", [], 3.0, True)
    text = path.read_text()
    assert cfg.render.ai_label_text in text
    assert "Hook (x)" in text  # braces escaped
    srt = subtitles.write_srt(cues, tmp_path / "c.srt").read_text()
    assert "00:00:00,000 --> " in srt


def test_brand_watermark(tmp_path: Path, cfg: config.Config) -> None:
    cues = subtitles.time_cues("hello world", 3.0, 4)
    text = subtitles.write_ass(
        cues, tmp_path / "w.ass", cfg.render, "h", [], 3.0, False
    ).read_text()
    assert "Brand,,0,0,0,,@secretmapclub" in text


def test_align_cues_snaps_to_pauses_and_beats() -> None:
    text = "Five details. One: a radio DJ. Two: a hidden apartment."
    # Voice pauses late after sentence 1 and 2 (an even split would be early).
    pauses = [(2.0, 2.6), (5.4, 6.0)]
    cues = subtitles.align_cues(text, 9.0, 4, pauses)
    one = next(c for c in cues if c.text.startswith("One:"))
    two = next(c for c in cues if c.text.startswith("Two:"))
    assert one.start == 2.6 and two.start == 6.0
    starts = subtitles.beat_starts(["5 DETAILS", "1. Radio DJ", "2. Apartment"], cues)
    assert starts == [0.0, 2.6, 6.0]


def test_align_without_pauses_matches_even_split() -> None:
    text = "Hello there. General Kenobi."
    a = subtitles.align_cues(text, 4.0, 4, [])
    assert a[0].start == 0.0 and abs(a[-1].end - 4.0) < 0.01
