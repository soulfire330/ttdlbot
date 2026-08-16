"""Основной пайплайн: кэш → метаданные → скачивание → file_id в Telegram."""

import asyncio
import hashlib
import logging
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Video
from cachetools import TTLCache

from ttblow.config import DEFAULT_TEMP_DIR, ServiceConfig, setting
from ttblow.downloader.extractor import extract_metadata
from ttblow.downloader.media import Job, download_video, validate_video
from ttblow.downloader.slideshow import download_images, download_slideshow
from ttblow.services.cache import FileIdCache
from ttblow.utils.urls import normalized_url, source_name

logger = logging.getLogger(__name__)


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
        # Telegram clients sometimes send /start twice on switch_pm tap
        self.claimed = TTLCache(
            maxsize=int(setting("PM_TASK_MAXSIZE", "10000")),
            ttl=300,
        )
        self.jobs = asyncio.Semaphore(int(setting("MAX_CONCURRENT_JOBS", "2")))
        self.inflight: dict[str, asyncio.Task] = {}
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
        self.claimed[task_id] = user_id
        return task[1]

    def was_claimed(self, task_id: str, user_id: int) -> bool:
        return self.claimed.get(task_id) == user_id

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
