"""Точка входа: инициализация и запуск бота."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from ttblow.bot.handlers import router
from ttblow.config import ServiceConfig, required, setting
from ttblow.services.cache import FileIdCache
from ttblow.services.video_service import VideoService
from ttblow.utils.fs import cleanup_stale_temp_dirs

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


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
    service = VideoService(
        bot,
        cache,
        ServiceConfig(
            proxy, int(cache_chat_id), int(setting("ADMIN_CHAT_ID", "0") or 0)
        ),
    )
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
