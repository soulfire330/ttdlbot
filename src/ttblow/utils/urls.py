"""Чистые функции парсинга и валидации URL."""

import re
from urllib.parse import urlparse

TIKTOK_SHORT_LINK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
YOUTUBE_SHORTS_PATH = re.compile(r"^/shorts/[A-Za-z0-9_-]{11}/?$")


def is_tiktok_host(host: str) -> bool:
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_youtube_host(host: str) -> bool:
    return host == "youtube.com" or host.endswith(".youtube.com")


def media_url(text: str) -> str | None:
    url = text.strip().split()[0] if text.strip() else ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not url.lower().startswith(("http://", "https://")):
        return None
    if is_tiktok_host(host):
        return url
    if is_youtube_host(host) and YOUTUBE_SHORTS_PATH.match(parsed.path):
        return url
    if (
        host == "instagram.com" or host.endswith(".instagram.com")
    ) and parsed.path.lower().startswith(("/reel/", "/reels/")):
        return url
    return None


def first_media_url(text: str) -> str | None:
    """Первая медиа-ссылка в тексте: в guest-режиме перед ней стоит упоминание бота."""
    for token in text.split():
        url = media_url(token)
        if url:
            return url
    return None


def source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if is_tiktok_host(host):
        return "tiktok"
    if is_youtube_host(host):
        return "youtube"
    return "instagram"


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
