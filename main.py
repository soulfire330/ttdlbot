import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultCachedVideo,
    InputMediaVideo,
    Message,
    Video,
)
from cachetools import TTLCache
from diskcache import Cache
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_MAX_DURATION = 600
DEFAULT_TEMP_DIR = "/tmp"
DEFAULT_TEMP_TTL = 24 * 60 * 60
DEFAULT_RAM_CACHE_TTL = 24 * 60 * 60
DEFAULT_DISK_CACHE_TTL = 30 * 24 * 60 * 60
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
IMAGE_DOWNLOAD_WORKERS = 4
SLIDESHOW_WIDTH = 640
SLIDESHOW_HEIGHT = 1280
SLIDESHOW_OUTPUT_FPS = 10
FFMPEG_ERROR_TAIL = 500
TIKTOK_SHORT_LINK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}


@dataclass(frozen=True)
class Job:
    url: str
    proxy: str | None
    directory: Path


@dataclass(frozen=True)
class ServiceConfig:
    proxy: str | None
    cache_chat_id: int


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def required(name: str) -> str:
    value = setting(name)
    if not value:
        raise SystemExit(f"Укажите {name}")
    return value


def is_tiktok_host(host: str) -> bool:
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def media_url(text: str) -> str | None:
    url = text.strip().split()[0] if text.strip() else ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not url.lower().startswith(("http://", "https://")):
        return None
    if is_tiktok_host(host):
        return url
    if (
        host == "instagram.com" or host.endswith(".instagram.com")
    ) and parsed.path.lower().startswith(("/reel/", "/reels/")):
        return url
    return None


def source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "tiktok" if is_tiktok_host(host) else "instagram"


def normalized_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def tiktok_photo_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not is_tiktok_host(host):
        return None
    if not re.fullmatch(r"/@[\w.-]+/photo/\d+/?", parsed.path):
        return None
    return parsed._replace(path=parsed.path.replace("/photo/", "/video/", 1)).geturl()


def tiktok_video_id(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def resolve_url(url: str, proxy: str | None) -> str:
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        response = ydl.urlopen(url)
        try:
            return response.url
        finally:
            response.close()


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


def cleanup_stale_temp_dirs() -> None:
    root = Path(setting("TEMP_DIR", DEFAULT_TEMP_DIR))
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - int(setting("TEMP_TTL", str(DEFAULT_TEMP_TTL)))
    for path in root.glob("ttblow-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError as error:
            logger.warning("Failed to remove stale temp directory %s: %s", path, error)


def extractor_options(
    proxy: str | None, directory: Path | None = None
) -> dict[str, Any]:
    options = {
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": int(setting("YTDLP_SOCKET_TIMEOUT", "30")),
        "retries": int(setting("YTDLP_RETRIES", "1")),
        "fragment_retries": int(setting("YTDLP_RETRIES", "1")),
        "max_filesize": int(setting("MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE))),
    }
    if proxy:
        options["proxy"] = proxy
    if directory:
        options["outtmpl"] = str(Path(directory) / "%(id)s.%(ext)s")
    return options


def tiktok_photo_info(photo_url: str, proxy: str | None) -> dict[str, Any]:
    extractor, raw_info, status = tiktok_aweme_data(photo_url, proxy)
    if status or not raw_info:
        raise ValueError(f"TikTok photo is unavailable (status {status})")
    info = extractor._parse_aweme_video_web(
        raw_info, photo_url, tiktok_video_id(photo_url)
    )
    info["image_urls"] = [
        {
            "url": image["imageURL"]["urlList"][0],
            "width": image.get("imageWidth"),
            "height": image.get("imageHeight"),
        }
        for image in raw_info.get("imagePost", {}).get("images", [])
        if image.get("imageURL", {}).get("urlList")
    ]
    if not info["image_urls"]:
        raise ValueError("TikTok photo has no downloadable images")
    info["media_type"] = "photo"
    return info


def tiktok_aweme_data(
    url: str, proxy: str | None
) -> tuple[Any, dict[str, Any] | None, int]:
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        extractor = ydl.get_info_extractor("TikTok")
        raw_info, status = extractor._extract_web_data_and_status(
            url, tiktok_video_id(url)
        )
        return extractor, raw_info, status


def extract_metadata(url: str, proxy: str | None) -> dict[str, Any]:
    photo_url = tiktok_photo_url(url)
    if not photo_url and urlparse(url).hostname in TIKTOK_SHORT_LINK_HOSTS:
        photo_url = tiktok_photo_url(resolve_url(url, proxy))
    if not photo_url:
        with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
            return ydl.extract_info(url, download=False)
    return tiktok_photo_info(photo_url, proxy)


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
    if image_count <= 0 or duration <= 0:
        raise ValueError("slideshow needs images and a positive duration")
    return image_count / duration


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


def download_slideshow(
    info: dict[str, Any], images: list[Path], job: Job
) -> tuple[dict[str, Any], Path]:
    source_audio = audio_url(info)
    duration = float(info.get("duration") or 0)
    if not source_audio:
        raise ValueError("TikTok photo has no downloadable audio")
    frame_rate = slideshow_frame_rate(len(images), duration)
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
    run_ffmpeg(
        [
            "-framerate",
            str(frame_rate),
            "-start_number",
            "1",
            "-i",
            str(job.directory / "%d.jpg"),
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
        ],
        output_path,
    )
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
    }
    validate_video(info, output_path)
    return info, output_path


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


def mux_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    run_ffmpeg(
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
    return output_path


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


def video_key(info: dict[str, Any], url: str) -> str:
    key = str(info.get("id") or hashlib.sha256(url.encode()).hexdigest()[:32])
    return f"{source_name(url)}:{key}"


def video_record(info: dict[str, Any], video: Video) -> dict[str, Any]:
    return {
        "file_id": video.file_id,
        "title": (info.get("title") or "TikTok video")[:256],
        "description": (f"@{info['uploader']}" if info.get("uploader") else "TikTok")[
            :255
        ],
        "video_width": video.width,
        "video_height": video.height,
        "video_duration": video.duration,
    }


class FileIdCache:
    def __init__(self) -> None:
        self.ram = TTLCache(
            maxsize=int(setting("RAM_CACHE_MAXSIZE", "10000")),
            ttl=int(setting("RAM_CACHE_TTL", str(DEFAULT_RAM_CACHE_TTL))),
        )
        self.disk = Cache(setting("DISK_CACHE_DIR", "data/cache"))
        self.disk_ttl = int(setting("DISK_CACHE_TTL", str(DEFAULT_DISK_CACHE_TTL)))

    async def get_with_source(
        self, key: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        record = self.ram.get(key)
        if record is not None:
            return record, "ram"

        record = await asyncio.to_thread(self.disk.get, key)
        if record is not None:
            self.ram[key] = record
            return record, "disk"
        return None, None

    async def get(self, key: str) -> dict[str, Any] | None:
        record, _ = await self.get_with_source(key)
        return record

    async def set(self, key: str, record: dict[str, Any]) -> None:
        self.ram[key] = record
        await asyncio.to_thread(
            self.disk.set,
            key,
            record,
            expire=self.disk_ttl,
        )

    async def delete(self, key: str) -> None:
        self.ram.pop(key, None)
        await asyncio.to_thread(self.disk.delete, key)

    async def close(self) -> None:
        await asyncio.to_thread(self.disk.close)


class VideoService:
    def __init__(self, bot: Bot, cache: FileIdCache, config: ServiceConfig) -> None:
        self.bot = bot
        self.cache = cache
        self.config = config
        self.inline_timeout = min(int(setting("INLINE_TIMEOUT", "9")), 9)
        self.telegram_timeout = int(setting("TELEGRAM_REQUEST_TIMEOUT", "120"))
        self.pm_urls = TTLCache(
            maxsize=int(setting("PM_TASK_MAXSIZE", "10000")),
            ttl=int(setting("PM_TASK_TTL", str(24 * 60 * 60))),
        )
        self.jobs = asyncio.Semaphore(int(setting("MAX_CONCURRENT_JOBS", "2")))
        self.inflight = {}
        self.rate_limit = TTLCache(
            maxsize=int(setting("RATE_LIMIT_USERS", "10000")),
            ttl=int(setting("RATE_LIMIT_WINDOW", "60")),
        )
        self.rate_limit_count = int(setting("RATE_LIMIT_COUNT", "10"))
        self.rate_limit_lock = asyncio.Lock()

    async def allow_user(self, user_id: int) -> bool:
        async with self.rate_limit_lock:
            count = self.rate_limit.get(user_id, 0)
            if count >= self.rate_limit_count:
                return False
            self.rate_limit[user_id] = count + 1
            return True

    def register_pm_task(self, url: str, user_id: int) -> str:
        task_id = secrets.token_urlsafe(24)
        while task_id in self.pm_urls:
            task_id = secrets.token_urlsafe(24)
        self.pm_urls[task_id] = (user_id, url)
        return task_id

    def claim_pm_url(self, task_id: str, user_id: int) -> str | None:
        task = self.pm_urls.get(task_id)
        if not task or task[0] != user_id:
            return None
        self.pm_urls.pop(task_id, None)
        return task[1]

    async def result_for(self, url: str) -> tuple[str, dict[str, Any]]:
        request_key = normalized_url(url)
        task = self.inflight.get(request_key)
        if task is None:
            task = asyncio.create_task(self._resolve(url))
            self.inflight[request_key] = task
            task.add_done_callback(lambda _: self.inflight.pop(request_key, None))
        return await asyncio.shield(task)

    async def cached_record_if_valid(
        self, key: str, record: dict[str, Any], source: str | None
    ) -> dict[str, Any] | None:
        if record.get("type") == "photo":
            await self.cache.delete(key)
            return None
        if source == "disk":
            try:
                await self.bot.get_file(record["file_id"])
            except TelegramBadRequest:
                await self.cache.delete(key)
                return None
        return record

    async def _cached_record(self, key: str) -> dict[str, Any] | None:
        record, source = await self.cache.get_with_source(key)
        if record is None:
            return None
        return await self.cached_record_if_valid(key, record, source)

    async def _resolve(self, url: str) -> tuple[str, dict[str, Any]]:
        async with self.jobs:
            job_start = time.perf_counter()
            alias = f"alias:{normalized_url(url)}"
            key = await self.cache.get(alias)
            if key:
                record = await self._cached_record(key)
                if record is not None:
                    logger.info("Cache hit for %s media %s", source_name(url), key)
                    return key, record
                await self.cache.delete(alias)

            stage_start = time.perf_counter()
            metadata = await asyncio.to_thread(extract_metadata, url, self.config.proxy)
            key = video_key(metadata, url)
            logger.info(
                "Prepared TikTok metadata %s in %.2fs",
                key,
                time.perf_counter() - stage_start,
            )
            record = await self._cached_record(key)
            if record is not None:
                await self.cache.set(alias, key)
                logger.info("Cache hit for %s media %s", source_name(url), key)
                return key, record

            record = await self._fetch_and_cache(url, metadata, key)
            logger.info(
                "Cached %s media %s as Telegram file_id in %.2fs",
                source_name(url),
                key,
                time.perf_counter() - job_start,
            )
            return key, record

    async def _fetch_and_cache(
        self, url: str, metadata: dict[str, Any], key: str
    ) -> dict[str, Any]:
        alias = f"alias:{normalized_url(url)}"
        logger.info("Cache miss for %s media %s", source_name(url), key)
        with tempfile.TemporaryDirectory(
            prefix="ttblow-",
            dir=setting("TEMP_DIR", DEFAULT_TEMP_DIR),
        ) as directory:
            job = Job(url, self.config.proxy, Path(directory))
            if metadata.get("media_type") == "photo":
                stage_start = time.perf_counter()
                images = await asyncio.to_thread(
                    download_images, metadata["image_urls"], job
                )
                logger.info(
                    "Downloaded %d TikTok images %s in %.2fs",
                    len(images),
                    key,
                    time.perf_counter() - stage_start,
                )
                info, path = await asyncio.to_thread(
                    download_slideshow, metadata, images, job
                )
            else:
                stage_start = time.perf_counter()
                validate_video(metadata)
                info, path = await asyncio.to_thread(download_video, job)
                logger.info(
                    "Downloaded TikTok video %s in %.2fs",
                    key,
                    time.perf_counter() - stage_start,
                )
            stage_start = time.perf_counter()
            message = await self.bot.send_video(
                chat_id=self.config.cache_chat_id,
                video=FSInputFile(path),
                supports_streaming=True,
                request_timeout=self.telegram_timeout,
            )
            record = video_record(info, message.video)
            logger.info(
                "Uploaded Telegram video %s in %.2fs",
                key,
                time.perf_counter() - stage_start,
            )
            await self.cache.set(key, record)
            await self.cache.set(alias, key)
        return record

    async def close(self) -> None:
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()


router = Router()


def cached_result(key: str, record: dict[str, Any]) -> InlineQueryResultCachedVideo:
    return InlineQueryResultCachedVideo(
        id=f"video:{key}",
        video_file_id=record["file_id"],
        title=record["title"],
        description=record["description"],
        video_width=record.get("video_width"),
        video_height=record.get("video_height"),
        video_duration=record.get("video_duration"),
    )


def report_background_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error:
        logger.error("Timed-out video job failed: %s", error)


@router.inline_query()
async def inline_query(query: InlineQuery, service: VideoService) -> None:
    text = query.query or ""
    logger.info("Inline query %s received (%d chars)", query.id, len(text))
    url = media_url(text)
    switch_pm_text = None
    switch_pm_parameter = None
    if not url:
        results = []
    elif not await service.allow_user(query.from_user.id):
        logger.warning("Rate limit exceeded for user %s", query.from_user.id)
        results = []
    else:
        task = asyncio.create_task(service.result_for(url))
        try:
            key, record = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=service.inline_timeout,
            )
            results = [cached_result(key, record)]
        except TimeoutError:
            task.add_done_callback(report_background_failure)
            switch_pm_text = "⏳ Видео долго обрабатывается. Отправить в ЛС."
            switch_pm_parameter = service.register_pm_task(url, query.from_user.id)
            logger.warning(
                "Inline query %s timed out after %ss; task %s moved to PM",
                query.id,
                service.inline_timeout,
                switch_pm_parameter,
            )
            results = []
        except Exception as error:
            logger.error("Failed to prepare inline query %s: %s", query.id, error)
            results = []

    try:
        await query.answer(
            results,
            cache_time=0,
            is_personal=True,
            switch_pm_text=switch_pm_text,
            switch_pm_parameter=switch_pm_parameter,
        )
        logger.info(
            "Answered inline query %s with %d result(s)", query.id, len(results)
        )
    except Exception as error:
        logger.error("Failed to answer inline query %s: %s", query.id, error)


@router.message(CommandStart())
async def private_start(
    message: Message, command: CommandObject, service: VideoService
) -> None:
    if message.chat.type != "private":
        return
    url = service.claim_pm_url(command.args or "", message.from_user.id)
    if not url:
        await message.answer(
            "Ссылка устарела. Отправьте её через inline-режим ещё раз."
        )
        return
    placeholder = await message.answer("⏳ Загрузка...")
    try:
        _, record = await service.result_for(url)
    except Exception as error:
        logger.error(
            "Failed to process private video for task %s: %s", command.args, error
        )
        await placeholder.edit_text(
            "❌ Не удалось обработать видео. Попробуйте ещё раз."
        )
        return
    try:
        await service.bot.edit_message_media(
            chat_id=message.chat.id,
            message_id=placeholder.message_id,
            media=InputMediaVideo(media=record["file_id"]),
        )
    except Exception as error:
        logger.error("Failed to edit placeholder for task %s: %s", command.args, error)
        await service.bot.send_video(
            chat_id=message.chat.id,
            video=record["file_id"],
        )
        await placeholder.delete()
    logger.info("Sent task %s to private chat %s", command.args, message.chat.id)


def make_dispatcher(service: VideoService) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["service"] = service
    return dispatcher


async def main() -> None:
    cleanup_stale_temp_dirs()
    token = required("TELEGRAM_BOT_TOKEN")
    cache_chat_id = required("TELEGRAM_CACHE_CHAT_ID")
    proxy = setting("YTDLP_PROXY")
    telegram_proxy = setting("TELEGRAM_PROXY") or proxy
    bot = Bot(token, session=AiohttpSession(proxy=telegram_proxy))
    cache = FileIdCache()
    service = VideoService(bot, cache, ServiceConfig(proxy, int(cache_chat_id)))
    dispatcher = make_dispatcher(service)

    logger.info(
        "Starting bot; yt-dlp proxy=%s; Telegram proxy=%s; cache=%s",
        proxy,
        telegram_proxy,
        setting("DISK_CACHE_DIR", "data/cache"),
    )

    try:
        await dispatcher.start_polling(bot, allowed_updates=["inline_query", "message"])
    finally:
        await service.close()
        await cache.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
