import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import uvloop
import yt_dlp
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultCachedVideo,
    Message,
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
        host == "instagram.com" or host.endswith(".instagram.com")
    ) and parsed.path.lower().startswith(("/reel/", "/reels/")):
        return url
    return None


def source_name(url):
    host = (urlparse(url).hostname or "").lower()
    return "instagram" if "instagram.com" in host else "tiktok"


def normalized_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def tiktok_photo_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return None
    if not re.fullmatch(r"/@[\w.-]+/photo/\d+/?", parsed.path):
        return None
    return parsed._replace(path=parsed.path.replace("/photo/", "/video/", 1)).geturl()


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
    photo_url = tiktok_photo_url(url)
    host = (urlparse(url).hostname or "").lower()
    if not photo_url and host in {"vm.tiktok.com", "vt.tiktok.com"}:
        with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
            response = ydl.urlopen(url)
            try:
                photo_url = tiktok_photo_url(response.url)
            finally:
                response.close()
    if not photo_url:
        with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
            return ydl.extract_info(url, download=False)

    video_id = urlparse(photo_url).path.rstrip("/").rsplit("/", 1)[-1]
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        extractor = ydl.get_info_extractor("TikTok")
        raw_info, status = extractor._extract_web_data_and_status(photo_url, video_id)
        if status or not raw_info:
            raise ValueError(f"TikTok photo is unavailable (status {status})")
        info = extractor._parse_aweme_video_web(raw_info, photo_url, video_id)
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


def download_file(url, proxy, path):
    max_size = int(setting("MAX_FILE_SIZE", str(50 * 1024 * 1024)))
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        response = ydl.urlopen(url)
        size = 0
        try:
            with path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError(f"file size exceeds {max_size} bytes")
                    output.write(chunk)
        finally:
            response.close()
    return path


def download_images(images, proxy, directory):
    def download(index_image):
        index, image = index_image
        return image, download_file(
            image["url"], proxy, Path(directory) / f"{index}.jpg"
        )

    with ThreadPoolExecutor(max_workers=min(4, len(images))) as executor:
        return list(executor.map(download, enumerate(images, 1)))


def audio_url(info):
    return next(
        (
            format_info["url"]
            for format_info in info.get("formats", [])
            if format_info.get("vcodec") == "none" and format_info.get("url")
        ),
        None,
    )


def slideshow_frame_rate(image_count, duration):
    if image_count <= 0 or duration <= 0:
        raise ValueError("slideshow needs images and a positive duration")
    return image_count / duration


def download_slideshow(info, images, proxy, directory):
    source_audio = audio_url(info)
    duration = float(info.get("duration") or 0)
    if not source_audio:
        raise ValueError("TikTok photo has no downloadable audio")
    frame_rate = slideshow_frame_rate(len(images), duration)
    stage_started = time.perf_counter()
    audio_path = download_file(source_audio, proxy, Path(directory) / "audio.mp3")
    logger.info(
        "Downloaded TikTok audio %s in %.2fs",
        info.get("id"),
        time.perf_counter() - stage_started,
    )
    output_path = Path(directory) / "slideshow.mp4"
    stage_started = time.perf_counter()
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                str(frame_rate),
                "-start_number",
                "1",
                "-i",
                str(Path(directory) / "%d.jpg"),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                "scale=640:1280:force_original_aspect_ratio=decrease,pad=640:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-r",
                "10",
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
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ValueError("ffmpeg не установлен") from error
    if result.returncode or not output_path.is_file():
        error = result.stderr.strip()[-500:]
        raise ValueError(f"ffmpeg не собрал слайдшоу: {error}")
    logger.info(
        "Built TikTok slideshow %s in %.2fs (%d bytes)",
        info.get("id"),
        time.perf_counter() - stage_started,
        output_path.stat().st_size,
    )
    info = {**info, "ext": "mp4", "width": 640, "height": 1280}
    validate_video(info, output_path)
    return info, output_path


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
        "description": (f"@{info['uploader']}" if info.get("uploader") else "TikTok")[
            :255
        ],
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

    async def allow_user(self, user_id):
        async with self.rate_limit_lock:
            count = self.rate_limit.get(user_id, 0)
            if count >= self.rate_limit_count:
                return False
            self.rate_limit[user_id] = count + 1
            return True

    def pm_task_id(self, url):
        task_id = hashlib.sha256(normalized_url(url).encode()).hexdigest()[:32]
        self.pm_urls[task_id] = url
        return task_id

    def pm_url(self, task_id):
        return self.pm_urls.get(task_id)

    async def result_for(self, url):
        request_key = normalized_url(url)
        task = self.inflight.get(request_key)
        if task is None:
            task = asyncio.create_task(self._resolve(url))
            self.inflight[request_key] = task
            task.add_done_callback(lambda _: self.inflight.pop(request_key, None))
        return await asyncio.shield(task)

    async def valid_cached_record(self, key, record, source):
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

    async def _resolve(self, url):
        async with self.jobs:
            job_started = time.perf_counter()
            alias = f"alias:{normalized_url(url)}"
            key = await self.cache.get(alias)
            if key:
                record, source = await self.cache.get_with_source(key)
                if record is not None:
                    record = await self.valid_cached_record(key, record, source)
                    if record is not None:
                        logger.info("Cache hit for %s media %s", source_name(url), key)
                        return key, record
                await self.cache.delete(alias)

            stage_started = time.perf_counter()
            metadata = await asyncio.to_thread(extract_metadata, url, self.proxy)
            key = video_key(metadata, url)
            logger.info(
                "Prepared TikTok metadata %s in %.2fs",
                key,
                time.perf_counter() - stage_started,
            )
            record, source = await self.cache.get_with_source(key)
            if record is not None:
                record = await self.valid_cached_record(key, record, source)
                if record is not None:
                    await self.cache.set(alias, key)
                    logger.info("Cache hit for %s media %s", source_name(url), key)
                    return key, record

            logger.info("Cache miss for %s media %s", source_name(url), key)
            with tempfile.TemporaryDirectory(
                prefix="ttblow-",
                dir=setting("TEMP_DIR", "/tmp"),
            ) as directory:
                if metadata.get("media_type") == "photo":
                    stage_started = time.perf_counter()
                    images = await asyncio.to_thread(
                        download_images,
                        metadata["image_urls"],
                        self.proxy,
                        directory,
                    )
                    logger.info(
                        "Downloaded %d TikTok images %s in %.2fs",
                        len(images),
                        key,
                        time.perf_counter() - stage_started,
                    )
                    info, path = await asyncio.to_thread(
                        download_slideshow,
                        metadata,
                        images,
                        self.proxy,
                        directory,
                    )
                else:
                    stage_started = time.perf_counter()
                    validate_video(metadata)
                    info, path = await asyncio.to_thread(
                        download_video,
                        url,
                        self.proxy,
                        directory,
                    )
                    logger.info(
                        "Downloaded TikTok video %s in %.2fs",
                        key,
                        time.perf_counter() - stage_started,
                    )
                stage_started = time.perf_counter()
                message = await self.bot.send_video(
                    chat_id=self.cache_chat_id,
                    video=FSInputFile(path),
                    supports_streaming=True,
                    request_timeout=self.telegram_timeout,
                )
                record = video_record(info, message.video.file_id)
                logger.info(
                    "Uploaded Telegram video %s in %.2fs",
                    key,
                    time.perf_counter() - stage_started,
                )
                await self.cache.set(key, record)
                await self.cache.set(alias, key)
            logger.info(
                "Cached %s media %s as Telegram file_id in %.2fs",
                source_name(url),
                key,
                time.perf_counter() - job_started,
            )
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
            switch_pm_text = "⏳ Видео слишком длинное. Отправить в ЛС."
            switch_pm_parameter = service.pm_task_id(url)
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
):
    url = service.pm_url(command.args or "")
    if not url:
        await message.answer(
            "Ссылка устарела. Отправьте её через inline-режим ещё раз."
        )
        return
    try:
        _, record = await service.result_for(url)
        await service.bot.send_video(
            chat_id=message.chat.id,
            video=record["file_id"],
            caption="Готово!",
        )
        logger.info("Sent task %s to private chat %s", command.args, message.chat.id)
    except Exception as error:
        logger.error(
            "Failed to send private video for task %s: %s", command.args, error
        )
        await message.answer("❌ Не удалось обработать видео. Попробуйте ещё раз.")


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
        allowed_updates=["inline_query", "message"],
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
        await dispatcher.start_polling(bot, allowed_updates=["inline_query", "message"])
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
