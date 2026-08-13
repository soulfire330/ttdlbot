"""Двухуровневое кэширование file_id: RAM (TTLCache) + диск (diskcache)."""

import asyncio
from typing import Any

from cachetools import TTLCache
from diskcache import Cache

from ttblow.config import DEFAULT_DISK_CACHE_TTL, DEFAULT_RAM_CACHE_TTL, setting


class FileIdCache:
    def __init__(self) -> None:
        self.ram = TTLCache(
            maxsize=int(setting("RAM_CACHE_MAXSIZE", "10000")),
            ttl=int(setting("RAM_CACHE_TTL", str(DEFAULT_RAM_CACHE_TTL))),
        )
        self.disk = Cache(setting("DISK_CACHE_DIR", "data/cache"))
        self.disk_ttl = int(setting("DISK_CACHE_TTL", str(DEFAULT_DISK_CACHE_TTL)))

    async def get_with_source(
        self, key: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        record = self.ram.get(key)
        if record is not None:
            return record, "ram"

        record = await asyncio.to_thread(self.disk.get, key)
        if record is not None:
            self.ram[key] = record
            return record, "disk"
        return None, None

    async def get(self, key: str) -> dict[str, Any] | None:
        record, _ = await self.get_with_source(key)
        return record

    async def set(self, key: str, record: dict[str, Any]) -> None:
        self.ram[key] = record
        await asyncio.to_thread(
            self.disk.set,
            key,
            record,
            expire=self.disk_ttl,
        )

    async def delete(self, key: str) -> None:
        self.ram.pop(key, None)
        await asyncio.to_thread(self.disk.delete, key)

    async def clear(self) -> None:
        self.ram.clear()
        await asyncio.to_thread(self.disk.clear)

    async def close(self) -> None:
        await asyncio.to_thread(self.disk.close)
