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


def require_libass() -> None:
    """Captions need FFmpeg built with libass (Homebrew: `brew install ffmpeg-full`)."""
    proc = subprocess.run(
        [require("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    )
    if not any(line.split()[1:2] == ["ass"] for line in proc.stdout.splitlines()):
        raise FfmpegError(
            "this FFmpeg has no libass (needed for captions). On macOS: "
            "`brew install ffmpeg-full` and put /opt/homebrew/opt/ffmpeg-full/bin first on PATH."
        )


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


def silences(path: Path, min_s: float = 0.2, noise_db: int = -35) -> list[tuple[float, float]]:
    """Pauses in an audio file as (start, end) seconds, via FFmpeg silencedetect."""
    proc = subprocess.run(
        [
            require("ffmpeg"),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_s}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[tuple[float, float]] = []
    start: float | None = None
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            start = float(line.rsplit("silence_start:", 1)[1].split()[0])
        elif "silence_end:" in line and start is not None:
            end = float(line.rsplit("silence_end:", 1)[1].split()[0])
            out.append((max(0.0, start), end))
            start = None
    return out
