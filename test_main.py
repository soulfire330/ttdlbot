import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import main


class MainTests(unittest.TestCase):
    def test_media_urls(self):
        self.assertTrue(main.media_url("https://vm.tiktok.com/abc/"))
        self.assertTrue(main.media_url("https://www.instagram.com/reel/ABC/"))
        self.assertTrue(main.media_url("https://instagram.com/reels/ABC/?igsh=x"))
        self.assertIsNone(main.media_url("https://www.instagram.com/p/ABC/"))
        self.assertIsNone(main.media_url("https://example.com/reel/ABC/"))

    def test_tiktok_photo_url(self):
        self.assertEqual(
            main.tiktok_photo_url("https://www.tiktok.com/@user/photo/123?_r=1"),
            "https://www.tiktok.com/@user/video/123?_r=1",
        )
        self.assertIsNone(
            main.tiktok_photo_url("https://www.tiktok.com/@user/video/123")
        )

    def test_slideshow_frame_rate(self):
        self.assertAlmostEqual(main.slideshow_frame_rate(8, 12), 2 / 3)
        with self.assertRaises(ValueError):
            main.slideshow_frame_rate(0, 12)

    def test_platform_cache_keys(self):
        self.assertEqual(
            main.video_key({"id": "123"}, "https://www.tiktok.com/@u/video/123"),
            "tiktok:123",
        )
        self.assertEqual(
            main.video_key({"id": "123"}, "https://www.instagram.com/reel/123/"),
            "instagram:123",
        )

    def test_pm_task_id_is_user_bound(self):
        service = main.VideoService(object(), object(), None, "chat")
        url = "https://www.tiktok.com/@user/video/123"
        task_id = service.pm_task_id(url, 42)
        self.assertEqual(len(task_id), 32)
        self.assertEqual(service.claim_pm_url(task_id, 42), url)
        self.assertIsNone(service.claim_pm_url(task_id, 42))

        other_task_id = service.pm_task_id(url, 42)
        self.assertIsNone(service.claim_pm_url(other_task_id, 43))
        self.assertEqual(service.claim_pm_url(other_task_id, 42), url)

    def test_private_start_rejects_other_user(self):
        async def check():
            service = main.VideoService(object(), object(), None, "chat")
            task_id = service.pm_task_id("https://www.tiktok.com/@u/video/123", 42)
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=43),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await main.private_start(message, command, service)
            self.assertEqual(len(answers), 1)
            self.assertIn("устарела", answers[0])

        asyncio.run(check())

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
                *(
                    service.result_for("https://www.tiktok.com/@u/video/123")
                    for _ in range(5)
                )
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

    def test_has_audio_stream_and_mux(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            silent = directory / "silent.mp4"
            audio = directory / "audio.m4a"
            merged = directory / "merged.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=1",
                    "-c:v",
                    "libx264",
                    str(silent),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "aac",
                    str(audio),
                ],
                check=True,
            )
            self.assertFalse(main.has_audio_stream(silent))
            self.assertTrue(main.has_audio_stream(audio))
            main.mux_audio(silent, audio, merged)
            self.assertTrue(main.has_audio_stream(merged))

    def test_video_record_uses_uploaded_video_dims(self):
        video = SimpleNamespace(file_id="abc", width=1080, height=1920, duration=30)
        record = main.video_record({"title": "t", "uploader": "u"}, video)
        self.assertEqual(record["file_id"], "abc")
        self.assertEqual(record["video_width"], 1080)
        self.assertEqual(record["video_height"], 1920)
        self.assertEqual(record["video_duration"], 30)

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
