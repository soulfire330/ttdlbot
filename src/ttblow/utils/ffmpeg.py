"""Обёртки над subprocess: ffmpeg и ffprobe."""

import subprocess
from fractions import Fraction
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


def _frame_rate(value: str) -> Fraction | None:
    numerator, separator, denominator = value.strip().partition("/")
    denominator = denominator if separator else "1"
    if not (numerator.isdigit() and denominator.isdigit()) or denominator == "0":
        return None
    rate = Fraction(int(numerator), int(denominator))
    return rate if rate else None


def is_vfr(path: Path) -> bool:
    """True, если у видео неравномерные таймстампы (VFR): nominal != avg fps."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    avg, _, nominal = result.stdout.strip().partition(",")
    avg_rate, nominal_rate = _frame_rate(avg), _frame_rate(nominal)
    return (
        avg_rate is not None and nominal_rate is not None and avg_rate != nominal_rate
    )


def normalize_cfr(path: Path, output_path: Path) -> Path:
    """Ре-энкод видео с равномерными таймстампами; звук копируется как есть."""
    return run_ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "copy",
            "-fps_mode",
            "cfr",
            str(output_path),
        ],
        output_path,
    )


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
