"""Извлечение метаданных через yt-dlp и парсинг API TikTok."""

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from ttblow.config import DEFAULT_MAX_FILE_SIZE, DOWNLOAD_CHUNK_SIZE, setting
from ttblow.utils.urls import TIKTOK_SHORT_LINK_HOSTS, tiktok_photo_url, tiktok_video_id


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
    cookies_file = setting("YTDLP_COOKIES_FILE")
    if cookies_file:
        options["cookiefile"] = cookies_file
    if directory:
        options["outtmpl"] = str(Path(directory) / "%(id)s.%(ext)s")
    return options


def resolve_url(url: str, proxy: str | None) -> str:
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        response = ydl.urlopen(url)
        try:
            return response.url
        finally:
            response.close()


def tiktok_aweme_data(
    url: str, proxy: str | None
) -> tuple[Any, dict[str, Any] | None, int]:
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        extractor = ydl.get_info_extractor("TikTok")
        raw_info, status = extractor._extract_web_data_and_status(
            url, tiktok_video_id(url)
        )
        return extractor, raw_info, status


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


def download_tiktok_mix(url: str, proxy: str | None, path: Path) -> Path | None:
    """Скачивает watermarked-файл с полным миксом звука.

    CDN требует cookies и заголовки сеанса, в котором была получена подпись
    URL, поэтому скачивание идёт через тот же ydl, что извлекал метаданные.
    """
    with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
        extractor = ydl.get_info_extractor("TikTok")
        raw_info, status = extractor._extract_web_data_and_status(
            url, tiktok_video_id(url)
        )
        if status or not raw_info:
            return None
        info = extractor._parse_aweme_video_web(raw_info, url, tiktok_video_id(url))
        fmt = next(
            (f for f in info.get("formats", []) if f.get("format_id") == "download"),
            None,
        )
        if not fmt:
            return None
        max_size = int(setting("MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE)))
        request = yt_dlp.networking.Request(
            fmt["url"], headers=info.get("http_headers") or {}
        )
        response = ydl.urlopen(request)
        try:
            with path.open("wb") as output:
                size = 0
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError(f"file size exceeds {max_size} bytes")
                    output.write(chunk)
        finally:
            response.close()
        return path


def extract_metadata(url: str, proxy: str | None) -> dict[str, Any]:
    photo_url = tiktok_photo_url(url)
    if not photo_url and urlparse(url).hostname in TIKTOK_SHORT_LINK_HOSTS:
        photo_url = tiktok_photo_url(resolve_url(url, proxy))
    if not photo_url:
        with yt_dlp.YoutubeDL(extractor_options(proxy)) as ydl:
            return ydl.extract_info(url, download=False)
    return tiktok_photo_info(photo_url, proxy)
