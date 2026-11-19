"""Voiceover generation. Returns the audio path and its USD cost."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx

from gce import media
from gce.config import TtsCfg, secret


def estimated_seconds(text: str, words_per_minute: int) -> float:
    return max(3.0, len(text.split()) / words_per_minute * 60)


def synthesize(text: str, out: Path, cfg: TtsCfg, language: str = "en") -> tuple[Path, float]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if cfg.provider == "silent":
        target = out.with_suffix(".wav")
        secs = estimated_seconds(text, cfg.words_per_minute)
        media.run(
            ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{secs:.2f}", str(target)]
        )
        return target, 0.0
    if cfg.provider == "say":
        binary = shutil.which("say")
        if not binary:
            raise RuntimeError("`say` provider needs macOS")
        voice = cfg.say_voices.get(language, cfg.say_voices.get("en", "Samantha"))
        aiff = out.with_suffix(".aiff")
        proc = subprocess.run(
            [binary, "-v", voice, "-r", str(cfg.say_rate), "-o", str(aiff), text],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"say failed (voice {voice!r} installed?): {proc.stderr.strip()}")
        target = out.with_suffix(".wav")
        media.run(["-i", str(aiff), "-ar", "48000", "-ac", "2", str(target)])
        return target, 0.0
    if cfg.provider == "piper":
        binary = shutil.which("piper")
        model = cfg.piper_models.get(language, cfg.piper_model)
        if not binary or not model:
            raise RuntimeError("piper provider needs the `piper` binary and tts.piper_model set")
        target = out.with_suffix(".wav")
        proc = subprocess.run(
            [binary, "--model", model, "--output_file", str(target)],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"piper failed: {proc.stderr.strip()}")
        return target, 0.0
    if cfg.provider == "openai":
        key = secret("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        target = out.with_suffix(".mp3")
        resp = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": cfg.openai_model,
                "voice": cfg.openai_voice,
                "input": text,
                "response_format": "mp3",
                "speed": cfg.openai_speed,
            },
            timeout=120,
        )
        resp.raise_for_status()
        target.write_bytes(resp.content)
        return target, len(text) / 1000 * cfg.openai_usd_per_1k_chars
    raise ValueError(f"unknown tts provider: {cfg.provider}")
