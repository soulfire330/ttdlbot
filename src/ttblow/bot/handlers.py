"""Бот-интерфейс: inline-запросы и команда /start."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultCachedVideo,
    InputMediaVideo,
    Message,
)

from ttblow.config import DEFAULT_COOKIES_FILE, setting
from ttblow.services.video_service import VideoService
from ttblow.utils.urls import media_url

logger = logging.getLogger(__name__)

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


def _is_admin(message: Message, service: VideoService) -> bool:
    return (
        service.config.admin_chat_id != 0
        and message.from_user is not None
        and message.from_user.id == service.config.admin_chat_id
    )


@router.message(Command("clear-cache"))
async def admin_clear_cache(message: Message, service: VideoService) -> None:
    if not _is_admin(message, service):
        return
    await service.cache.clear()
    await message.answer("✅ Кэш очищен. Видео будут скачаны заново.")
    logger.info("Cache cleared by admin %s", message.from_user.id)


@router.message(F.document)
async def admin_cookie_upload(message: Message, service: VideoService) -> None:
    if not _is_admin(message, service):
        return
    if (message.document.file_name or "").lower() not in ("cookie.txt", "cookies.txt"):
        return
    target = Path(setting("YTDLP_COOKIES_FILE") or DEFAULT_COOKIES_FILE)
    tmp = target.with_name(f"{target.name}.tmp")
    try:
        await message.bot.download(message.document, destination=tmp)
        tmp.replace(target)
    except Exception as error:
        logger.error("Failed to update cookies file: %s", error)
        await message.answer(f"❌ Не удалось записать cookies: {error}")
        return
    await message.answer(
        f"✅ Cookies обновлены ({message.document.file_size} байт). "
        "Новые запросы будут использовать их."
    )
    logger.info(
        "Cookies updated by admin %s (%d bytes)",
        message.from_user.id,
        message.document.file_size,
    )


@router.message(CommandStart())
async def private_start(
    message: Message, command: CommandObject, service: VideoService
) -> None:
    if message.chat.type != "private":
        return
    args = command.args or ""
    if not args:
        me = await message.bot.me()
        await message.answer(
            "Привет! Отправьте ссылку на TikTok или Instagram Reels — скачаю видео.\n\n"
            f"В любом чате можно через инлайн: @{me.username} <ссылка>"
        )
        return
    url = media_url(args)
    if url is None:
        url = service.claim_pm_url(args, message.from_user.id)
    if not url:
        if not service.was_claimed(args, message.from_user.id):
            await message.answer(
                "Ссылка устарела или уже обрабатывается. "
                "Отправьте её через инлайн ещё раз."
            )
        return
    await _process_private(message, service, url, args or url)


@router.message()
async def private_link(message: Message, service: VideoService) -> None:
    if message.chat.type != "private" or not message.text:
        return
    url = media_url(message.text)
    if not url:
        return
    if not await service.allow_user(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите немного.")
        return
    await _process_private(message, service, url, url)


async def _process_private(
    message: Message, service: VideoService, url: str, label: str
) -> None:
    placeholder = await message.answer("⏳ Загрузка...")
    try:
        _, record = await service.result_for(url)
    except Exception as error:
        logger.error("Failed to process private video %s: %s", label, error)
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
        logger.error("Failed to edit placeholder %s: %s", label, error)
        await service.bot.send_video(
            chat_id=message.chat.id,
            video=record["file_id"],
        )
        await placeholder.delete()
    logger.info("Sent %s to private chat %s", label, message.chat.id)
