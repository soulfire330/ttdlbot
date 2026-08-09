"""Скачивание видео, фото и восстановление звуковой дорожки."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from ttblow.config import (
    DEFAULT_MAX_DURATION,
    DEFAULT_MAX_FILE_SIZE,
    DOWNLOAD_CHUNK_SIZE,
    setting,
)
from ttblow.downloader.extractor import (
    extractor_options,
    resolve_url,
    tiktok_aweme_data,
)
from ttblow.utils.ffmpeg import has_audio_stream, mux_audio
from ttblow.utils.urls import source_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    url: str
    proxy: str | None
    directory: Path


def validate_video(info: dict[str, Any], path: Path | None = None) -> None:
    max_duration = int(setting("MAX_DURATION", str(DEFAULT_MAX_DURATION)))
    duration = info.get("duration")
    if duration and duration > max_duration:
        raise ValueError(f"video duration exceeds {max_duration}s")

    max_size = int(setting("MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE)))
    source_size = info.get("filesize") or info.get("filesize_approx")
    if source_size and source_size > max_size:
        raise ValueError(f"video size exceeds {max_size} bytes")
    if path and path.stat().st_size > max_size:
        raise ValueError(f"video size exceeds {max_size} bytes")


def download_file(url: str, proxy: str | None, path: Path) -> Path:
    max_size = int(setting("MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE)))
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        response = ydl.urlopen(url)
        size = 0
        try:
            with path.open("wb") as output:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError(f"file size exceeds {max_size} bytes")
                    output.write(chunk)
        finally:
            response.close()
    return path


def tiktok_music_url(url: str, proxy: str | None) -> str | None:
    _, raw_info, status = tiktok_aweme_data(resolve_url(url, proxy), proxy)
    if status or not raw_info:
        return None
    music = raw_info.get("music") or {}
    play_url = music.get("playUrl")
    if isinstance(play_url, str):
        return play_url
    urls = (play_url or {}).get("urlList") or []
    return urls[0] if urls else None


def restore_audio(video_path: Path, job: Job, info: dict[str, Any]) -> Path:
    """Webapp formats claim acodec but ship video-only; mux the music track."""
    try:
        music_url = tiktok_music_url(job.url, job.proxy)
        if not music_url:
            logger.warning(
                "No music track for audio-less TikTok video %s", info.get("id")
            )
            return video_path
        audio_path = download_file(music_url, job.proxy, job.directory / "music.m4a")
        output_path = mux_audio(video_path, audio_path, job.directory / "merged.mp4")
        validate_video(info, output_path)
        logger.info("Restored audio for TikTok video %s", info.get("id"))
        return output_path
    except Exception as error:
        # ponytail: best-effort — deliver silent video instead of failing request
        logger.warning("Failed to restore audio for %s: %s", info.get("id"), error)
        return video_path


def download_video(job: Job) -> tuple[dict[str, Any], Path]:
    with yt_dlp.YoutubeDL(extractor_options(job.proxy, job.directory)) as ydl:
        info = ydl.extract_info(job.url, download=True)
        path = Path(ydl.prepare_filename(info))

    if info.get("ext") != "mp4" or not path.is_file():
        raise ValueError("yt-dlp не скачал доступный mp4")
    validate_video(info, path)
    if source_name(job.url) == "tiktok" and not has_audio_stream(path):
        path = restore_audio(path, job, info)
    return info, path
