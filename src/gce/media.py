"""Small FFmpeg/ffprobe helpers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def require(binary: str = "ffmpeg") -> str:
    path = shutil.which(binary)
    if not path:
        raise FfmpegError(
            f"{binary} not found on PATH. Install FFmpeg (e.g. `brew install ffmpeg`)."
        )
    return path


def run(args: list[str], cwd: Path | None = None) -> None:
    cmd = [require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")


def duration(path: Path) -> float:
    proc = subprocess.run(
        [
            require("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise FfmpegError(f"ffprobe could not read duration of {path}: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def has_audio(path: Path) -> bool:
    proc = subprocess.run(
        [
            require("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())
