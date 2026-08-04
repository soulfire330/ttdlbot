import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import main


class MainTests(unittest.TestCase):
    def test_media_urls(self):
        self.assertTrue(main.media_url("https://vm.tiktok.com/abc/"))
        self.assertTrue(main.media_url("https://www.instagram.com/reel/ABC/"))
        self.assertTrue(main.media_url("https://instagram.com/reels/ABC/?igsh=x"))
        self.assertIsNone(main.media_url("https://www.instagram.com/p/ABC/"))
        self.assertIsNone(main.media_url("https://example.com/reel/ABC/"))

    def test_platform_cache_keys(self):
        self.assertEqual(
            main.video_key({"id": "123"}, "https://www.tiktok.com/@u/video/123"),
            "tiktok:123",
        )
        self.assertEqual(
            main.video_key({"id": "123"}, "https://www.instagram.com/reel/123/"),
            "instagram:123",
        )

    def test_singleflight(self):
        async def check():
            service = main.VideoService(object(), object(), None, "chat")
            calls = 0

            async def resolve(url):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.01)
                return "tiktok:123", {"file_id": "abc"}

            service._resolve = resolve
            results = await asyncio.gather(
                *(service.result_for("https://www.tiktok.com/@u/video/123") for _ in range(5))
            )
            self.assertEqual(calls, 1)
            self.assertEqual(results[0], results[-1])
            await service.close()

        asyncio.run(check())

    def test_cache_persists_to_disk(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                old_dir = os.environ.get("DISK_CACHE_DIR")
                os.environ["DISK_CACHE_DIR"] = str(Path(directory) / "cache")
                try:
                    first = main.FileIdCache()
                    await first.set("tiktok:123", {"file_id": "abc"})
                    await first.close()

                    second = main.FileIdCache()
                    self.assertEqual(
                        await second.get("tiktok:123"),
                        {"file_id": "abc"},
                    )
                    await second.close()
                finally:
                    if old_dir is None:
                        os.environ.pop("DISK_CACHE_DIR", None)
                    else:
                        os.environ["DISK_CACHE_DIR"] = old_dir

        asyncio.run(check())

    def test_rate_limit(self):
        async def check():
            service = main.VideoService(object(), object(), None, "chat")
            service.rate_limit_count = 2
            self.assertTrue(await service.allow_user(1))
            self.assertTrue(await service.allow_user(1))
            self.assertFalse(await service.allow_user(1))
            await service.close()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()