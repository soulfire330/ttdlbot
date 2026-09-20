"""Бот-интерфейс: guest-запросы, личка и админ-команды."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InlineQueryResultUnion,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
)

from ttblow.config import DEFAULT_COOKIES_FILE, setting
from ttblow.services.video_service import VideoService
from ttblow.utils.urls import first_media_url

logger = logging.getLogger(__name__)

router = Router()

background_tasks: set[asyncio.Task] = set()

LOADING_TEXT = "⏳ Загрузка..."
FAILURE_TEXT = "❌ Не удалось обработать видео. Попробуйте ещё раз."


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


def text_result(result_id: str, title: str, text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        input_message_content=InputTextMessageContent(message_text=text),
    )


async def hint_text(message: Message) -> str:
    me = await message.bot.me()
    return f"Пришлите ссылку на TikTok или Instagram Reels: @{me.username} <ссылка>"


async def answer_guest(
    service: VideoService, query_id: str, result: InlineQueryResultUnion
) -> str | None:
    try:
        sent = await service.bot.answer_guest_query(query_id, result)
    except Exception as error:
        logger.error("Failed to answer guest query %s: %s", query_id, error)
        return None
    logger.info(
        "Answered guest query %s with message %s", query_id, sent.inline_message_id
    )
    return sent.inline_message_id


async def edit_guest_text(
    service: VideoService, inline_message_id: str, text: str
) -> None:
    try:
        await service.bot.edit_message_text(
            text=text, inline_message_id=inline_message_id
        )
    except Exception as error:
        logger.error("Failed to edit guest message %s: %s", inline_message_id, error)


async def edit_guest_when_ready(
    service: VideoService, inline_message_id: str, url: str
) -> None:
    """Джоба качается в фоне: подменяем плейсхолдер видео или текстом ошибки."""
    try:
        _, record = await service.result_for(url)
    except Exception as error:
        logger.error("Failed to process guest video %s: %s", url, error)
        await edit_guest_text(service, inline_message_id, FAILURE_TEXT)
        return
    try:
        await service.bot.edit_message_media(
            media=InputMediaVideo(media=record["file_id"]),
            inline_message_id=inline_message_id,
        )
    except Exception as error:
        logger.error("Failed to edit guest message %s: %s", inline_message_id, error)
        await edit_guest_text(service, inline_message_id, FAILURE_TEXT)
        return
    logger.info("Sent %s as guest message %s", url, inline_message_id)


@router.guest_message()
async def guest_message(message: Message, service: VideoService) -> None:
    query_id = message.guest_query_id
    if not query_id:
        return
    logger.info("Guest query %s from user %s", query_id, message.from_user.id)
    if not await service.allow_user(message.from_user.id):
        logger.warning("Rate limit exceeded for user %s", message.from_user.id)
        return

    url = first_media_url(message.text or "")
    if url is None:
        await answer_guest(
            service,
            query_id,
            text_result("hint", "Подсказка", await hint_text(message)),
        )
        return

    cached = await service.cached_video(url)
    if cached is not None:
        await answer_guest(service, query_id, cached_result(*cached))
        return

    inline_message_id = await answer_guest(
        service, query_id, text_result("loading", "Загрузка", LOADING_TEXT)
    )
    if inline_message_id is None:
        return
    task = asyncio.create_task(edit_guest_when_ready(service, inline_message_id, url))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


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
async def private_start(message: Message, service: VideoService) -> None:
    if message.chat.type != "private":
        return
    url = first_media_url(message.text or "")
    if url:
        await _process_private(message, service, url)
        return
    me = await message.bot.me()
    await message.answer(
        "Привет! Отправьте ссылку на TikTok или Instagram Reels — скачаю видео.\n\n"
        f"В любом чате упомяните меня: @{me.username} <ссылка>"
    )


@router.message()
async def private_link(message: Message, service: VideoService) -> None:
    if message.chat.type != "private" or not message.text:
        return
    url = first_media_url(message.text)
    if not url:
        return
    if not await service.allow_user(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите немного.")
        return
    await _process_private(message, service, url)


async def _process_private(message: Message, service: VideoService, url: str) -> None:
    placeholder = await message.answer(LOADING_TEXT)
    try:
        _, record = await service.result_for(url)
    except Exception as error:
        logger.error("Failed to process private video %s: %s", url, error)
        await placeholder.edit_text(FAILURE_TEXT)
        return
    try:
        await service.bot.edit_message_media(
            chat_id=message.chat.id,
            message_id=placeholder.message_id,
            media=InputMediaVideo(media=record["file_id"]),
        )
    except Exception as error:
        logger.error("Failed to edit placeholder %s: %s", url, error)
        await service.bot.send_video(
            chat_id=message.chat.id,
            video=record["file_id"],
        )
        await placeholder.delete()
    logger.info("Sent %s to private chat %s", url, message.chat.id)
