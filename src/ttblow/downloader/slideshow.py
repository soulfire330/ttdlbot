"""Сборка слайдшоу в mp4 через FFmpeg."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ttblow.config import (
    IMAGE_DOWNLOAD_WORKERS,
    SLIDESHOW_HEIGHT,
    SLIDESHOW_MAX_FRAME_DURATION,
    SLIDESHOW_MIN_FRAME_DURATION,
    SLIDESHOW_OUTPUT_FPS,
    SLIDESHOW_WIDTH,
)
from ttblow.downloader.media import Job, download_file, validate_video
from ttblow.utils.ffmpeg import media_duration, run_ffmpeg

logger = logging.getLogger(__name__)


def download_images(images: list[dict[str, Any]], job: Job) -> list[Path]:
    def download(index_image: tuple[int, dict[str, Any]]) -> Path:
        index, image = index_image
        return download_file(image["url"], job.proxy, job.directory / f"{index}.jpg")

    with ThreadPoolExecutor(
        max_workers=min(IMAGE_DOWNLOAD_WORKERS, len(images))
    ) as executor:
        return list(executor.map(download, enumerate(images, 1)))


def audio_url(info: dict[str, Any]) -> str | None:
    return next(
        (
            format_info["url"]
            for format_info in info.get("formats", [])
            if format_info.get("vcodec") == "none" and format_info.get("url")
        ),
        None,
    )


def slideshow_frame_rate(image_count: int, duration: float) -> float:
    """FPS с длиной кадра 2–4 с: image_count/duration, ограниченная по краям."""
    if image_count <= 0 or duration <= 0:
        raise ValueError("slideshow needs images and a positive duration")
    rate = image_count / duration
    return max(
        1 / SLIDESHOW_MAX_FRAME_DURATION,
        min(1 / SLIDESHOW_MIN_FRAME_DURATION, rate),
    )


def download_slideshow(
    info: dict[str, Any], images: list[Path], job: Job
) -> tuple[dict[str, Any], Path]:
    source_audio = audio_url(info)
    source_duration = float(info.get("duration") or 0)
    if not source_audio:
        raise ValueError("TikTok photo has no downloadable audio")
    frame_rate = slideshow_frame_rate(len(images), source_duration)
    stage_start = time.perf_counter()
    audio_path = download_file(source_audio, job.proxy, job.directory / "audio.mp3")
    logger.info(
        "Downloaded TikTok audio %s in %.2fs",
        info.get("id"),
        time.perf_counter() - stage_start,
    )
    output_path = job.directory / "slideshow.mp4"
    stage_start = time.perf_counter()
    scale_filter = (
        f"scale={SLIDESHOW_WIDTH}:{SLIDESHOW_HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={SLIDESHOW_WIDTH}:{SLIDESHOW_HEIGHT}:"
        "(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    target_duration = len(images) / frame_rate
    arguments = [
        "-framerate",
        str(frame_rate),
        "-start_number",
        "1",
        "-i",
        str(job.directory / "%d.jpg"),
    ]
    # ponytail: -shortest режет длинное аудио; короткое зацикливаем
    if media_duration(audio_path) < target_duration:
        arguments += ["-stream_loop", "-1"]
    arguments += [
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        scale_filter,
        "-r",
        str(SLIDESHOW_OUTPUT_FPS),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "stillimage",
        "-crf",
        "30",
        "-threads",
        "0",
        "-c:a",
        "copy",
        "-shortest",
        str(output_path),
    ]
    run_ffmpeg(arguments, output_path)
    logger.info(
        "Built TikTok slideshow %s in %.2fs (%d bytes)",
        info.get("id"),
        time.perf_counter() - stage_start,
        output_path.stat().st_size,
    )
    info = {
        **info,
        "ext": "mp4",
        "width": SLIDESHOW_WIDTH,
        "height": SLIDESHOW_HEIGHT,
        "duration": target_duration,
    }
    validate_video(info, output_path)
    return info, output_path
