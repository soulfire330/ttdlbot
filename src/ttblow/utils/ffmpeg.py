"""Обёртки над subprocess: ffmpeg и ffprobe."""

import subprocess
from pathlib import Path

from ttblow.config import FFMPEG_ERROR_TAIL


def run_ffmpeg(arguments: list[str], output_path: Path) -> Path:
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ValueError("ffmpeg не установлен") from error
    if result.returncode or not output_path.is_file():
        error = result.stderr.strip()[-FFMPEG_ERROR_TAIL:]
        raise ValueError(f"ffmpeg не собрал {output_path.name}: {error}")
    return output_path


def has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def mux_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    return run_ffmpeg(
        [
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(output_path),
        ],
        output_path,
    )
