import asyncio
import hashlib
import hmac
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import uvloop
import yt_dlp
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultCachedVideo,
    Update,
)
from aiohttp import web
from cachetools import TTLCache
from diskcache import Cache
from dotenv import load_dotenv


load_dotenv()
uvloop.install()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_PATH = "/telegram/webhook"


def setting(name, default=None):
    return os.environ.get(name, default)


def required(name):
    value = setting(name)
    if not value:
        raise SystemExit(f"Укажите {name}")
    return value


def media_url(text):
    url = text.strip().split()[0] if text.strip() else ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not url.startswith(("http://", "https://")):
        return None
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return url
    if (
        (host == "instagram.com" or host.endswith(".instagram.com"))
        and parsed.path.lower().startswith(("/reel/", "/reels/"))
    ):
        return url
    return None


def source_name(url):
    host = (urlparse(url).hostname or "").lower()
    return "instagram" if "instagram.com" in host else "tiktok"


def normalized_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def validate_video(info, path=None):
    max_duration = int(setting("MAX_DURATION", "600"))
    duration = info.get("duration")
    if duration and duration > max_duration:
        raise ValueError(f"video duration exceeds {max_duration}s")

    max_size = int(setting("MAX_FILE_SIZE", str(50 * 1024 * 1024)))
    source_size = info.get("filesize") or info.get("filesize_approx")
    if source_size and source_size > max_size:
        raise ValueError(f"video size exceeds {max_size} bytes")
    if path and path.stat().st_size > max_size:
        raise ValueError(f"video size exceeds {max_size} bytes")


def cleanup_stale_temp_dirs():
    root = Path(setting("TEMP_DIR", "/tmp"))
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - int(setting("TEMP_TTL", str(24 * 60 * 60)))
    for path in root.glob("ttblow-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError as error:
            logger.warning("Failed to remove stale temp directory %s: %s", path, error)


def extractor_options(proxy, directory=None):
    options = {
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": int(setting("YTDLP_SOCKET_TIMEOUT", "30")),
        "retries": int(setting("YTDLP_RETRIES", "1")),
        "fragment_retries": int(setting("YTDLP_RETRIES", "1")),
            "max_filesize": int(setting("MAX_FILE_SIZE", str(50 * 1024 * 1024))),
    }
    if proxy:
        options["proxy"] = proxy
    if directory:
        options["outtmpl"] = str(Path(directory) / "%(id)s.%(ext)s")
    return options


def extract_metadata(url, proxy):
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(url, proxy, directory):
    with yt_dlp.YoutubeDL(extractor_options(proxy, directory)) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))

    if info.get("ext") != "mp4" or not path.is_file():
        raise ValueError("yt-dlp не скачал доступный mp4")
    validate_video(info, path)
    return info, path


def video_key(info, url):
    key = str(info.get("id") or hashlib.sha256(url.encode()).hexdigest()[:32])
    return f"{source_name(url)}:{key}"


def video_record(info, file_id):
    return {
        "file_id": file_id,
        "title": (info.get("title") or "TikTok video")[:256],
        "description": (
            f"@{info['uploader']}" if info.get("uploader") else "TikTok"
        )[:255],
        "video_width": info.get("width"),
        "video_height": info.get("height"),
        "video_duration": int(info["duration"]) if info.get("duration") else None,
    }


class FileIdCache:
    def __init__(self):
        self.ram = TTLCache(
            maxsize=int(setting("RAM_CACHE_MAXSIZE", "10000")),
            ttl=int(setting("RAM_CACHE_TTL", str(24 * 60 * 60))),
        )
        self.disk = Cache(setting("DISK_CACHE_DIR", "data/cache"))
        self.disk_ttl = int(setting("DISK_CACHE_TTL", str(30 * 24 * 60 * 60)))

    async def get_with_source(self, key):
        record = self.ram.get(key)
        if record is not None:
            return record, "ram"

        record = await asyncio.to_thread(self.disk.get, key)
        if record is not None:
            self.ram[key] = record
            return record, "disk"
        return None, None

    async def get(self, key):
        record, _ = await self.get_with_source(key)
        return record

    async def set(self, key, record):
        self.ram[key] = record
        await asyncio.to_thread(
            self.disk.set,
            key,
            record,
            expire=self.disk_ttl,
        )

    async def delete(self, key):
        self.ram.pop(key, None)
        await asyncio.to_thread(self.disk.delete, key)

    async def close(self):
        await asyncio.to_thread(self.disk.close)


class VideoService:
    def __init__(self, bot, cache, proxy, cache_chat_id):
        self.bot = bot
        self.cache = cache
        self.proxy = proxy
        self.cache_chat_id = cache_chat_id
        self.inline_timeout = int(setting("INLINE_TIMEOUT", "20"))
        self.telegram_timeout = int(setting("TELEGRAM_REQUEST_TIMEOUT", "120"))
        self.jobs = asyncio.Semaphore(int(setting("MAX_CONCURRENT_JOBS", "2")))
        self.inflight = {}
        self.rate_limit = TTLCache(
            maxsize=int(setting("RATE_LIMIT_USERS", "10000")),
            ttl=int(setting("RATE_LIMIT_WINDOW", "60")),
        )
        self.rate_limit_count = int(setting("RATE_LIMIT_COUNT", "10"))
        self.rate_limit_lock = asyncio.Lock()

    async def allow_user(self, user_id):
        async with self.rate_limit_lock:
            count = self.rate_limit.get(user_id, 0)
            if count >= self.rate_limit_count:
                return False
            self.rate_limit[user_id] = count + 1
            return True

    async def result_for(self, url):
        request_key = normalized_url(url)
        task = self.inflight.get(request_key)
        if task is None:
            task = asyncio.create_task(self._resolve(url))
            self.inflight[request_key] = task
            task.add_done_callback(lambda _: self.inflight.pop(request_key, None))
        return await asyncio.shield(task)

    async def _resolve(self, url):
        async with self.jobs:
            alias = f"alias:{normalized_url(url)}"
            key = await self.cache.get(alias)
            if key:
                record, source = await self.cache.get_with_source(key)
                if record is not None:
                    if source == "disk":
                        try:
                            await self.bot.get_file(record["file_id"])
                        except TelegramBadRequest:
                            await self.cache.delete(key)
                            await self.cache.delete(alias)
                            record = None
                    if record is not None:
                        logger.info("Cache hit for %s video %s", source_name(url), key)
                        return key, record
                await self.cache.delete(alias)

            metadata = await asyncio.to_thread(extract_metadata, url, self.proxy)
            key = video_key(metadata, url)
            record, source = await self.cache.get_with_source(key)
            if record is not None:
                if source == "disk":
                    try:
                        await self.bot.get_file(record["file_id"])
                    except TelegramBadRequest:
                        await self.cache.delete(key)
                        record = None
                if record is not None:
                    await self.cache.set(alias, key)
                    logger.info("Cache hit for %s video %s", source_name(url), key)
                    return key, record

            logger.info("Cache miss for %s video %s", source_name(url), key)
            validate_video(metadata)
            with tempfile.TemporaryDirectory(
                prefix="ttblow-",
                dir=setting("TEMP_DIR", "/tmp"),
            ) as directory:
                info, path = await asyncio.to_thread(
                    download_video,
                    url,
                    self.proxy,
                    directory,
                )
                message = await self.bot.send_video(
                    chat_id=self.cache_chat_id,
                    video=FSInputFile(path),
                    supports_streaming=True,
                    request_timeout=self.telegram_timeout,
                )
                record = video_record(info, message.video.file_id)
                await self.cache.set(key, record)
                await self.cache.set(alias, key)
            logger.info("Cached %s video %s as Telegram file_id", source_name(url), key)
            return key, record

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()


router = Router()


def cached_result(key, record):
    return InlineQueryResultCachedVideo(
        id=f"video:{key}",
        video_file_id=record["file_id"],
        title=record["title"],
        description=record["description"],
        video_width=record.get("video_width"),
        video_height=record.get("video_height"),
        video_duration=record.get("video_duration"),
    )


def report_background_failure(task):
    if task.cancelled():
        return
    error = task.exception()
    if error:
        logger.error("Timed-out video job failed: %s", error)


@router.inline_query()
async def inline_query(query: InlineQuery, service: VideoService):
    text = query.query or ""
    logger.info("Inline query %s received (%d chars)", query.id, len(text))
    url = media_url(text)
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
        except asyncio.TimeoutError:
            task.add_done_callback(report_background_failure)
            logger.warning(
                "Inline query %s timed out after %ss",
                query.id,
                service.inline_timeout,
            )
            results = []
        except Exception as error:
            logger.error("Failed to prepare inline query %s: %s", query.id, error)
            results = []

    try:
        await query.answer(results, cache_time=0, is_personal=True)
        logger.info("Answered inline query %s with %d result(s)", query.id, len(results))
    except Exception as error:
        logger.error("Failed to answer inline query %s: %s", query.id, error)


def make_dispatcher(service):
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["service"] = service
    return dispatcher


async def webhook_handler(request):
    secret = required("TELEGRAM_WEBHOOK_SECRET")
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not received or not hmac.compare_digest(received, secret):
        raise web.HTTPUnauthorized()

    bot = request.app["bot"]
    dispatcher = request.app["dispatcher"]
    update = Update.model_validate(await request.json())
    await dispatcher.feed_update(bot, update)
    return web.json_response({"ok": True})


async def health_handler(request):
    return web.json_response({"ok": True})


async def start_webhook(app):
    bot = app["bot"]
    base_url = required("PUBLIC_BASE_URL").rstrip("/")
    path = setting("WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
    await bot.set_webhook(
        f"{base_url}{path}",
        secret_token=required("TELEGRAM_WEBHOOK_SECRET"),
        allowed_updates=["inline_query"],
    )
    logger.info("Webhook configured at %s%s", base_url, path)


async def cleanup(app):
    await app["service"].close()
    await app["cache"].close()
    await app["bot"].session.close()


def create_app(bot, dispatcher, cache, service):
    app = web.Application(
        client_max_size=int(setting("WEBHOOK_MAX_BODY_SIZE", str(1024 * 1024)))
    )
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app["cache"] = cache
    app["service"] = service
    path = setting("WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
    app.router.add_post(path, webhook_handler)
    app.router.add_get("/healthz", health_handler)
    app.on_startup.append(start_webhook)
    app.on_cleanup.append(cleanup)
    return app


async def run_polling(bot, dispatcher, cache, service):
    try:
        await dispatcher.start_polling(bot, allowed_updates=["inline_query"])
    finally:
        await service.close()
        await cache.close()
        await bot.session.close()


def main():
    cleanup_stale_temp_dirs()
    token = required("TELEGRAM_BOT_TOKEN")
    cache_chat_id = required("TELEGRAM_CACHE_CHAT_ID")
    proxy = setting("YTDLP_PROXY")
    telegram_proxy = setting("TELEGRAM_PROXY") or proxy
    bot = Bot(token, session=AiohttpSession(proxy=telegram_proxy))
    cache = FileIdCache()
    service = VideoService(bot, cache, proxy, cache_chat_id)
    dispatcher = make_dispatcher(service)

    logger.info(
        "Starting bot; yt-dlp proxy=%s; Telegram proxy=%s; cache=%s",
        proxy,
        telegram_proxy,
        setting("DISK_CACHE_DIR", "data/cache"),
    )

    if setting("BOT_MODE", "webhook") == "polling":
        asyncio.run(run_polling(bot, dispatcher, cache, service))
        return

    app = create_app(bot, dispatcher, cache, service)
    web.run_app(
        app,
        host=setting("WEB_SERVER_HOST", "127.0.0.1"),
        port=int(setting("WEB_SERVER_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
