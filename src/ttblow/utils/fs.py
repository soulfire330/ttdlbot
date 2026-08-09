"""Утилиты работы с файловой системой."""

import logging
import shutil
import time
from pathlib import Path

from ttblow.config import DEFAULT_TEMP_DIR, DEFAULT_TEMP_TTL, setting

logger = logging.getLogger(__name__)


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
