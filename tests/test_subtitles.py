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
