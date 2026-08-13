"""Конфигурация приложения: константы и переменные окружения."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_MAX_DURATION = 600
DEFAULT_COOKIES_FILE = "/data/cache/cookies.txt"
DEFAULT_TEMP_DIR = "/tmp"
DEFAULT_TEMP_TTL = 24 * 60 * 60
DEFAULT_RAM_CACHE_TTL = 24 * 60 * 60
DEFAULT_DISK_CACHE_TTL = 30 * 24 * 60 * 60
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
IMAGE_DOWNLOAD_WORKERS = 4
SLIDESHOW_WIDTH = 640
SLIDESHOW_HEIGHT = 1280
SLIDESHOW_OUTPUT_FPS = 10
SLIDESHOW_MIN_FRAME_DURATION = 2.0
SLIDESHOW_MAX_FRAME_DURATION = 4.0
FFMPEG_ERROR_TAIL = 500


@dataclass(frozen=True)
class ServiceConfig:
    proxy: str | None
    cache_chat_id: int
    admin_chat_id: int = 0


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def required(name: str) -> str:
    value = setting(name)
    if not value:
        raise SystemExit(f"Укажите {name}")
    return value
