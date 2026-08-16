import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from ttblow import config, main as entry
from ttblow.bot import handlers
from ttblow.downloader import extractor, media, slideshow
from ttblow.services import cache, video_service
from ttblow.utils import ffmpeg, fs, urls


@contextmanager
def env(name, value):
    old_value = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old_value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old_value


class MainTests(unittest.TestCase):
    def test_media_urls(self):
        self.assertTrue(urls.media_url("https://vm.tiktok.com/abc/"))
        self.assertTrue(urls.media_url("https://www.instagram.com/reel/ABC/"))
        self.assertTrue(urls.media_url("https://instagram.com/reels/ABC/?igsh=x"))
        self.assertIsNone(urls.media_url("https://www.instagram.com/p/ABC/"))
        self.assertIsNone(urls.media_url("https://example.com/reel/ABC/"))

    def test_tiktok_photo_url(self):
        self.assertEqual(
            urls.tiktok_photo_url("https://www.tiktok.com/@user/photo/123?_r=1"),
            "https://www.tiktok.com/@user/video/123?_r=1",
        )
        self.assertIsNone(
            urls.tiktok_photo_url("https://www.tiktok.com/@user/video/123")
        )

    def test_slideshow_frame_rate(self):
        self.assertAlmostEqual(slideshow.slideshow_frame_rate(8, 24), 1 / 3)
        # 1.5 с/кадр → минимум 2 с, 15 с/кадр → максимум 4 с
        self.assertAlmostEqual(slideshow.slideshow_frame_rate(8, 12), 0.5)
        self.assertAlmostEqual(slideshow.slideshow_frame_rate(2, 30), 0.25)
        with self.assertRaises(ValueError):
            slideshow.slideshow_frame_rate(0, 12)

    def test_platform_cache_keys(self):
        self.assertEqual(
            video_service.video_key(
                {"id": "123"}, "https://www.tiktok.com/@u/video/123"
            ),
            "tiktok:123",
        )
        self.assertEqual(
            video_service.video_key(
                {"id": "123"}, "https://www.instagram.com/reel/123/"
            ),
            "instagram:123",
        )

    def test_pm_task_is_user_bound(self):
        service = video_service.VideoService(
            object(), object(), config.ServiceConfig(None, 0)
        )
        url = "https://www.tiktok.com/@user/video/123"
        task_id = service.register_pm_task(url, 42)
        self.assertEqual(len(task_id), 32)
        self.assertEqual(service.claim_pm_url(task_id, 42), url)
        self.assertIsNone(service.claim_pm_url(task_id, 42))

        other_task_id = service.register_pm_task(url, 42)
        self.assertIsNone(service.claim_pm_url(other_task_id, 43))
        self.assertEqual(service.claim_pm_url(other_task_id, 42), url)

    def test_private_start_rejects_other_user(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            task_id = service.register_pm_task(
                "https://www.tiktok.com/@u/video/123", 42
            )
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=43),
                bot=SimpleNamespace(
                    me=AsyncMock(return_value=SimpleNamespace(username="my_bot"))
                ),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await handlers.private_start(message, command, service)
            self.assertEqual(len(answers), 1)
            self.assertIn("Ссылка устарела", answers[0])

        asyncio.run(check())

    def test_private_start_stale_task_id(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            task_id = service.register_pm_task(
                "https://www.tiktok.com/@u/video/123", 42
            )
            service.claim_pm_url(task_id, 42)  # already claimed
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                bot=SimpleNamespace(me=AsyncMock()),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await handlers.private_start(message, command, service)
            self.assertEqual(len(answers), 0)  # duplicate /start is ignored
            message.bot.me.assert_not_called()

        asyncio.run(check())

    def test_private_start_unknown_task_id(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                bot=SimpleNamespace(me=AsyncMock()),
                answer=answer,
            )
            command = SimpleNamespace(args="missing-token")
            await handlers.private_start(message, command, service)
            self.assertEqual(len(answers), 1)
            self.assertIn("Ссылка устарела", answers[0])

        asyncio.run(check())

    def test_private_start_help(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.result_for = AsyncMock()
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                bot=SimpleNamespace(
                    me=AsyncMock(return_value=SimpleNamespace(username="my_bot"))
                ),
                answer=answer,
            )
            await handlers.private_start(message, SimpleNamespace(args=""), service)
            self.assertEqual(len(answers), 1)
            self.assertIn("Отправьте ссылку", answers[0])
            self.assertIn("@my_bot", answers[0])
            service.result_for.assert_not_called()

        asyncio.run(check())

    def test_private_start_with_url(self):
        async def check():
            edited = []

            async def edit_message_media(**kwargs):
                edited.append(kwargs)

            bot = SimpleNamespace(edit_message_media=edit_message_media)
            service = video_service.VideoService(
                bot, object(), config.ServiceConfig(None, 0)
            )
            service.result_for = AsyncMock(
                return_value=("tiktok:123", {"file_id": "abc"})
            )

            async def answer(text):
                return SimpleNamespace(
                    message_id=1, edit_text=AsyncMock(), delete=AsyncMock()
                )

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                answer=answer,
            )
            command = SimpleNamespace(args="https://www.tiktok.com/@u/video/123")
            await handlers.private_start(message, command, service)
            service.result_for.assert_called_once_with(
                "https://www.tiktok.com/@u/video/123"
            )
            self.assertEqual(edited[0]["media"].media, "abc")

        asyncio.run(check())

    def test_private_link_success(self):
        async def check():
            edited = []

            async def edit_message_media(**kwargs):
                edited.append(kwargs)

            bot = SimpleNamespace(edit_message_media=edit_message_media)
            service = video_service.VideoService(
                bot, object(), config.ServiceConfig(None, 0)
            )
            service.result_for = AsyncMock(
                return_value=("tiktok:123", {"file_id": "abc"})
            )

            async def answer(text):
                return SimpleNamespace(
                    message_id=1, edit_text=AsyncMock(), delete=AsyncMock()
                )

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                text="https://www.tiktok.com/@u/video/123",
                answer=answer,
            )
            await handlers.private_link(message, service)
            service.result_for.assert_called_once_with(
                "https://www.tiktok.com/@u/video/123"
            )
            self.assertEqual(edited[0]["media"].media, "abc")

        asyncio.run(check())

    def test_private_link_ignores_plain_text(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.result_for = AsyncMock()
            service.allow_user = AsyncMock()

            async def answer(text):
                raise AssertionError("must not answer")

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                text="привет",
                answer=answer,
            )
            await handlers.private_link(message, service)
            service.result_for.assert_not_called()

        asyncio.run(check())

    def test_private_link_rate_limited(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.allow_user = AsyncMock(return_value=False)
            service.result_for = AsyncMock()
            answers = []

            async def answer(text):
                answers.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                text="https://www.tiktok.com/@u/video/123",
                answer=answer,
            )
            await handlers.private_link(message, service)
            self.assertEqual(len(answers), 1)
            self.assertIn("много запросов", answers[0])
            service.result_for.assert_not_called()

        asyncio.run(check())

    def test_cache_clear(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    store = cache.FileIdCache()
                    await store.set("k", {"file_id": "abc"})
                    self.assertEqual((await store.get("k"))["file_id"], "abc")
                    await store.clear()
                    self.assertIsNone(await store.get("k"))
                    await store.close()

        asyncio.run(check())

    def test_clear_cache_requires_admin(self):
        async def check():
            service = video_service.VideoService(
                object(),
                SimpleNamespace(clear=AsyncMock()),
                config.ServiceConfig(None, 0, admin_chat_id=42),
            )
            answers = []

            async def answer(text):
                answers.append(text)

            await handlers.admin_clear_cache(
                SimpleNamespace(from_user=SimpleNamespace(id=42), answer=answer),
                service,
            )
            service.cache.clear.assert_awaited_once()
            self.assertIn("очищен", answers[0])

            await handlers.admin_clear_cache(
                SimpleNamespace(from_user=SimpleNamespace(id=43), answer=answer),
                service,
            )
            self.assertEqual(service.cache.clear.await_count, 1)
            self.assertEqual(len(answers), 1)  # stranger gets no reply at all

        asyncio.run(check())

    def test_admin_cookie_upload(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "cookies.txt"
                with env("YTDLP_COOKIES_FILE", str(target)):
                    service = video_service.VideoService(
                        object(),
                        object(),
                        config.ServiceConfig(None, 0, admin_chat_id=42),
                    )
                    answers = []

                    async def answer(text):
                        answers.append(text)

                    async def download(document, destination):
                        Path(destination).write_bytes(b"sessionid=abc")

                    document = SimpleNamespace(file_name="cookie.txt", file_size=13)
                    admin_message = SimpleNamespace(
                        from_user=SimpleNamespace(id=42),
                        document=document,
                        bot=SimpleNamespace(download=download),
                        answer=answer,
                    )
                    await handlers.admin_cookie_upload(admin_message, service)
                    self.assertEqual(target.read_bytes(), b"sessionid=abc")
                    self.assertIn("обновлены", answers[0])

                    plural = SimpleNamespace(
                        from_user=SimpleNamespace(id=42),
                        document=SimpleNamespace(file_name="COOKIES.txt", file_size=13),
                        bot=SimpleNamespace(download=download),
                        answer=answer,
                    )
                    await handlers.admin_cookie_upload(plural, service)
                    self.assertIn("обновлены", answers[1])
                    self.assertEqual(target.read_bytes(), b"sessionid=abc")

                    other = SimpleNamespace(
                        from_user=SimpleNamespace(id=42),
                        document=SimpleNamespace(file_name="video.mp4", file_size=13),
                        bot=SimpleNamespace(download=download),
                        answer=answer,
                    )
                    await handlers.admin_cookie_upload(other, service)
                    self.assertEqual(len(answers), 2)

                    stranger = SimpleNamespace(
                        from_user=SimpleNamespace(id=43),
                        document=document,
                        bot=SimpleNamespace(download=download),
                        answer=answer,
                    )
                    await handlers.admin_cookie_upload(stranger, service)
                    self.assertEqual(len(answers), 2)  # stranger gets no reply at all

                with mock.patch.object(
                    handlers,
                    "DEFAULT_COOKIES_FILE",
                    str(Path(directory) / "default.txt"),
                ):
                    await handlers.admin_cookie_upload(admin_message, service)
                    self.assertEqual(
                        (Path(directory) / "default.txt").read_bytes(), b"sessionid=abc"
                    )
                    self.assertIn("обновлены", answers[2])

                with env("YTDLP_COOKIES_FILE", str(target)):

                    async def fail_download(document, destination):
                        raise PermissionError("denied")

                    failing = SimpleNamespace(
                        from_user=SimpleNamespace(id=42),
                        document=document,
                        bot=SimpleNamespace(download=fail_download),
                        answer=answer,
                    )
                    await handlers.admin_cookie_upload(failing, service)
                    self.assertIn("Не удалось записать", answers[3])
                    self.assertEqual(target.read_bytes(), b"sessionid=abc")

        asyncio.run(check())

    def test_singleflight(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
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
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    first = cache.FileIdCache()
                    await first.set("tiktok:123", {"file_id": "abc"})
                    await first.close()

                    second = cache.FileIdCache()
                    self.assertEqual(
                        await second.get("tiktok:123"),
                        {"file_id": "abc"},
                    )
                    await second.close()

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
            self.assertFalse(media.has_audio_stream(silent))
            self.assertTrue(media.has_audio_stream(audio))
            media.mux_audio(silent, audio, merged)
            self.assertTrue(media.has_audio_stream(merged))

    def test_video_record_uses_uploaded_video_dims(self):
        video = SimpleNamespace(file_id="abc", width=1080, height=1920, duration=30)
        record = video_service.video_record({"title": "t", "uploader": "u"}, video)
        self.assertEqual(record["file_id"], "abc")
        self.assertEqual(record["video_width"], 1080)
        self.assertEqual(record["video_height"], 1920)
        self.assertEqual(record["video_duration"], 30)

    def test_rate_limit(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.rate_limit_count = 2
            self.assertTrue(await service.allow_user(1))
            self.assertTrue(await service.allow_user(1))
            self.assertFalse(await service.allow_user(1))
            await service.close()

        asyncio.run(check())

    def test_media_url_boundaries(self):
        self.assertIsNone(urls.media_url(""))
        self.assertIsNone(urls.media_url("   "))
        self.assertTrue(urls.media_url("HTTPS://www.tiktok.com/@u/video/1"))

    def test_normalized_url_normalizes_case(self):
        self.assertEqual(
            urls.normalized_url("HTTPS://WWW.TikTok.COM/@u/video/1/"),
            "https://www.tiktok.com/@u/video/1",
        )

    def test_validate_video_duration_and_size(self):
        with env("MAX_DURATION", "60"), env("MAX_FILE_SIZE", "100"):
            media.validate_video({"duration": 60, "filesize": 100})
            with self.assertRaises(ValueError):
                media.validate_video({"duration": 61})
            with self.assertRaises(ValueError):
                media.validate_video({"filesize": 101})

    def test_validate_video_checks_downloaded_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.mp4"
            path.write_bytes(b"x" * 101)
            with env("MAX_FILE_SIZE", "100"), self.assertRaises(ValueError):
                media.validate_video({}, path)

    def test_download_file_enforces_size_limit(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data

            def read(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

            def close(self):
                pass

        def fetch(data):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "file.bin"
                with env("MAX_FILE_SIZE", str(2 * 1024 * 1024)):
                    with mock.patch.object(media.yt_dlp, "YoutubeDL") as youtube_dl:
                        ydl = youtube_dl.return_value.__enter__.return_value
                        ydl.urlopen.return_value = FakeResponse(data)
                        media.download_file("https://tiktok.com/x", None, path)
                        return path.stat().st_size

        self.assertEqual(fetch(b"x" * (2 * 1024 * 1024)), 2 * 1024 * 1024)
        with self.assertRaises(ValueError):
            fetch(b"x" * (2 * 1024 * 1024 + 1))

    def test_cached_record_if_valid_drops_photo_records(self):
        async def check():
            deleted = []

            class StubCache:
                async def delete(self, key):
                    deleted.append(key)

            service = video_service.VideoService(
                object(), StubCache(), config.ServiceConfig(None, 0)
            )
            record = await service.cached_record_if_valid(
                "tiktok:1", {"type": "photo"}, None
            )
            self.assertIsNone(record)
            self.assertEqual(deleted, ["tiktok:1"])

        asyncio.run(check())

    def test_cached_record_if_valid_checks_telegram_file(self):
        async def check():
            deleted = []

            class StubCache:
                async def delete(self, key):
                    deleted.append(key)

            class StubBot:
                def __init__(self, missing):
                    self.missing = missing

                async def get_file(self, file_id):
                    if self.missing:
                        raise TelegramBadRequest(
                            method="getFile", message=f"file {file_id} not found"
                        )

            record = {"file_id": "abc", "type": "video"}
            broken = video_service.VideoService(
                StubBot(True), StubCache(), config.ServiceConfig(None, 0)
            )
            self.assertIsNone(
                await broken.cached_record_if_valid("tiktok:1", record, "disk")
            )
            self.assertEqual(deleted, ["tiktok:1"])

            working = video_service.VideoService(
                StubBot(False), StubCache(), config.ServiceConfig(None, 0)
            )
            self.assertEqual(
                await working.cached_record_if_valid("tiktok:1", record, "disk"),
                record,
            )

        asyncio.run(check())

    def test_required_missing_env(self):
        with self.assertRaises(SystemExit):
            config.required("TTBLOW_TEST_MISSING_ENV_VAR")

    def test_required_returns_env_value(self):
        with env("TTBLOW_TEST_ENV_VAR", "x"):
            self.assertEqual(config.required("TTBLOW_TEST_ENV_VAR"), "x")

    def test_resolve_url_follows_redirect(self):
        response = SimpleNamespace(
            url="https://www.tiktok.com/@u/video/123", close=lambda: None
        )
        with mock.patch.object(extractor.yt_dlp, "YoutubeDL") as youtube_dl:
            ydl = youtube_dl.return_value.__enter__.return_value
            ydl.urlopen.return_value = response
            self.assertEqual(
                extractor.resolve_url("https://vm.tiktok.com/abc", None),
                "https://www.tiktok.com/@u/video/123",
            )

    def test_tiktok_photo_url_rejects_non_tiktok_host(self):
        self.assertIsNone(urls.tiktok_photo_url("https://instagram.com/@u/photo/1"))

    def test_tiktok_video_id_strips_trailing_slash(self):
        self.assertEqual(
            urls.tiktok_video_id("https://www.tiktok.com/@u/video/123/"), "123"
        )

    def test_audio_url(self):
        formats = [
            {"vcodec": "none", "url": "https://sf/a.mp3"},
            {"vcodec": "h264", "url": "https://sf/v.mp4"},
        ]
        self.assertEqual(slideshow.audio_url({"formats": formats}), "https://sf/a.mp3")
        self.assertIsNone(slideshow.audio_url({"formats": [{"vcodec": "h264"}]}))
        self.assertIsNone(slideshow.audio_url({}))

    def test_extractor_options_proxy_and_directory(self):
        options = extractor.extractor_options("http://proxy:8080", Path("/tmp/out"))
        self.assertEqual(options["proxy"], "http://proxy:8080")
        self.assertEqual(options["outtmpl"], "/tmp/out/%(id)s.%(ext)s")
        plain = extractor.extractor_options(None)
        self.assertNotIn("proxy", plain)
        self.assertNotIn("outtmpl", plain)
        with tempfile.TemporaryDirectory() as directory:
            cookies = Path(directory) / "cookies.txt"
            cookies.write_bytes(b"")
            with env("YTDLP_COOKIES_FILE", str(cookies)):
                self.assertEqual(
                    extractor.extractor_options(None)["cookiefile"], str(cookies)
                )
            with env("YTDLP_COOKIES_FILE", str(Path(directory) / "missing.txt")):
                self.assertNotIn("cookiefile", extractor.extractor_options(None))
        with mock.patch.object(extractor.Path, "is_file", return_value=True):
            self.assertEqual(
                extractor.extractor_options(None)["cookiefile"],
                config.DEFAULT_COOKIES_FILE,
            )
        self.assertNotIn("cookiefile", extractor.extractor_options(None))

    def test_cached_result_fields(self):
        record = {
            "file_id": "abc",
            "title": "t",
            "description": "d",
            "video_width": 1080,
            "video_height": 1920,
            "video_duration": 30,
        }
        result = handlers.cached_result("tiktok:123", record)
        self.assertEqual(result.id, "video:tiktok:123")
        self.assertEqual(result.video_file_id, "abc")
        self.assertEqual(result.video_duration, 30)

    def test_make_dispatcher_work_data(self):
        service = object()
        dispatcher = entry.make_dispatcher(service)
        self.assertIs(dispatcher["service"], service)

    def test_file_id_cache_delete(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    file_id_cache = cache.FileIdCache()
                    await file_id_cache.set("tiktok:1", {"file_id": "abc"})
                    self.assertEqual(
                        (await file_id_cache.get_with_source("tiktok:1"))[1], "ram"
                    )
                    await file_id_cache.delete("tiktok:1")
                    record, source = await file_id_cache.get_with_source("tiktok:1")
                    self.assertIsNone(record)
                    self.assertIsNone(source)
                    await file_id_cache.close()

        asyncio.run(check())

    def test_cleanup_stale_temp_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "ttblow-old"
            fresh = root / "ttblow-fresh"
            other = root / "other"
            for path in (old, fresh, other):
                path.mkdir()
            old_time = time.time() - 100 * 24 * 60 * 60
            os.utime(old, (old_time, old_time))
            with env("TEMP_DIR", str(root)), env("TEMP_TTL", str(24 * 60 * 60)):
                fs.cleanup_stale_temp_dirs()
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())

    def test_cleanup_stale_temp_dirs_tolerates_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "ttblow-broken"
            stale.mkdir()
            old_time = time.time() - 100 * 24 * 60 * 60
            os.utime(stale, (old_time, old_time))
            with mock.patch.object(fs.shutil, "rmtree", side_effect=OSError("busy")):
                with env("TEMP_DIR", str(root)), env("TEMP_TTL", str(24 * 60 * 60)):
                    fs.cleanup_stale_temp_dirs()  # must not raise

    def test_run_ffmpeg_missing_and_failure(self):
        output = Path("/tmp/nonexistent.mp4")
        with mock.patch.object(ffmpeg.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(ValueError):
                ffmpeg.run_ffmpeg(["-i", "x"], output)
        with mock.patch.object(ffmpeg.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=1, stderr="boom\n")
            with self.assertRaises(ValueError) as ctx:
                ffmpeg.run_ffmpeg(["-i", "x"], output)
        self.assertIn("boom", str(ctx.exception))

    def test_download_images_numbers_files(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            job = media.Job("https://www.tiktok.com/@u/photo/1", None, directory)
            images = [{"url": "https://a/1.jpg"}, {"url": "https://b/2.jpg"}]
            with mock.patch.object(
                slideshow, "download_file", return_value=Path("stub.jpg")
            ) as download_file:
                paths = slideshow.download_images(images, job)
            self.assertEqual(len(paths), 2)
            download_file.assert_any_call("https://a/1.jpg", None, directory / "1.jpg")
            download_file.assert_any_call("https://b/2.jpg", None, directory / "2.jpg")

    def test_register_pm_task_collision(self):
        service = video_service.VideoService(
            object(), object(), config.ServiceConfig(None, 0)
        )
        service.pm_urls["dup"] = (1, "https://www.tiktok.com/@u/video/0")
        with mock.patch.object(
            video_service.secrets, "token_urlsafe", side_effect=["dup", "unique"]
        ):
            task_id = service.register_pm_task("https://www.tiktok.com/@u/video/123", 1)
        self.assertEqual(task_id, "unique")

    def test_tiktok_aweme_data(self):
        stub_extractor = SimpleNamespace(
            _extract_web_data_and_status=lambda url, vid: ({"id": vid}, 0)
        )
        with mock.patch.object(extractor.yt_dlp, "YoutubeDL") as youtube_dl:
            ydl = youtube_dl.return_value.__enter__.return_value
            ydl.get_info_extractor.return_value = stub_extractor
            extractor_obj, raw_info, status = extractor.tiktok_aweme_data(
                "https://www.tiktok.com/@u/photo/123", None
            )
        self.assertIs(extractor_obj, stub_extractor)
        self.assertEqual(raw_info, {"id": "123"})
        self.assertEqual(status, 0)

    def test_tiktok_photo_info(self):
        raw_info = {
            "imagePost": {
                "images": [
                    {
                        "imageURL": {"urlList": ["https://img/1.jpg"]},
                        "imageWidth": 100,
                    },
                    {"imageURL": {"urlList": ["https://img/2.jpg"]}},
                    {"noURL": True},
                ]
            }
        }
        stub_extractor = SimpleNamespace(
            _parse_aweme_video_web=lambda raw, url, vid: {"id": vid}
        )
        with mock.patch.object(
            extractor, "tiktok_aweme_data", return_value=(stub_extractor, raw_info, 0)
        ):
            info = extractor.tiktok_photo_info(
                "https://www.tiktok.com/@u/photo/123", None
            )
        self.assertEqual(info["media_type"], "photo")
        self.assertEqual(info["image_urls"][0]["url"], "https://img/1.jpg")
        self.assertEqual(len(info["image_urls"]), 2)

        with (
            mock.patch.object(
                extractor, "tiktok_aweme_data", return_value=(stub_extractor, None, 404)
            ),
            self.assertRaises(ValueError),
        ):
            extractor.tiktok_photo_info("https://www.tiktok.com/@u/photo/123", None)
        with (
            mock.patch.object(
                extractor,
                "tiktok_aweme_data",
                return_value=(stub_extractor, {"imagePost": {"images": []}}, 0),
            ),
            self.assertRaises(ValueError),
        ):
            extractor.tiktok_photo_info("https://www.tiktok.com/@u/photo/123", None)

    def test_extract_metadata_photo_path(self):
        with (
            mock.patch.object(
                extractor,
                "tiktok_photo_url",
                return_value="https://www.tiktok.com/@u/photo/123",
            ),
            mock.patch.object(
                extractor, "tiktok_photo_info", return_value={"id": "123"}
            ) as photo_info,
        ):
            info = extractor.extract_metadata(
                "https://www.tiktok.com/@u/photo/123", None
            )
        self.assertEqual(info, {"id": "123"})
        photo_info.assert_called_once_with("https://www.tiktok.com/@u/photo/123", None)

    def test_extract_metadata_resolves_short_link(self):
        with (
            mock.patch.object(
                extractor,
                "tiktok_photo_url",
                side_effect=[None, "https://www.tiktok.com/@u/photo/123"],
            ),
            mock.patch.object(
                extractor, "resolve_url", return_value="https://vm.tiktok.com/abc"
            ) as resolve,
        ):
            with mock.patch.object(extractor, "tiktok_photo_info", return_value={}):
                extractor.extract_metadata("https://vm.tiktok.com/abc", None)
        resolve.assert_called_once()

    def test_extract_metadata_video_path(self):
        with mock.patch.object(extractor, "tiktok_photo_url", return_value=None):
            with mock.patch.object(extractor.yt_dlp, "YoutubeDL") as youtube_dl:
                ydl = youtube_dl.return_value.__enter__.return_value
                ydl.extract_info.return_value = {"id": "123"}
                info = extractor.extract_metadata(
                    "https://www.tiktok.com/@u/video/123", None
                )
        self.assertEqual(info, {"id": "123"})

    def test_tiktok_music_url_variants(self):
        def music_url(raw, status):
            with (
                mock.patch.object(
                    media,
                    "resolve_url",
                    return_value="https://www.tiktok.com/@u/video/123",
                ),
                mock.patch.object(
                    media, "tiktok_aweme_data", return_value=(None, raw, status)
                ),
            ):
                return media.tiktok_music_url("https://vm.tiktok.com/abc", None)

        self.assertEqual(
            music_url({"music": {"playUrl": "https://sf/a.mp3"}}, 0),
            "https://sf/a.mp3",
        )
        self.assertEqual(
            music_url({"music": {"playUrl": {"urlList": ["https://sf/a.m4a"]}}}, 0),
            "https://sf/a.m4a",
        )
        self.assertIsNone(music_url({"music": {}}, 0))
        self.assertIsNone(music_url({}, 500))

    def test_download_tiktok_mix(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data

            def read(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

            def close(self):
                pass

        def fetch(status, raw, formats, data=b"mix"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "mix.mp4"
                ie = SimpleNamespace(
                    _extract_web_data_and_status=lambda *a: (raw, status),
                    _parse_aweme_video_web=lambda *a: {
                        "formats": formats,
                        "http_headers": {
                            "Referer": "https://www.tiktok.com/@u/video/123"
                        },
                    },
                )
                with mock.patch.object(extractor.yt_dlp, "YoutubeDL") as youtube_dl:
                    ydl = youtube_dl.return_value.__enter__.return_value
                    ydl.get_info_extractor.return_value = ie
                    ydl.urlopen.return_value = FakeResponse(data)
                    got = extractor.download_tiktok_mix(
                        "https://www.tiktok.com/@u/video/123", None, path
                    )
                    if got is not None:
                        got = (got.read_bytes(), ydl.urlopen.call_args.args[0].headers)
                return got

        self.assertEqual(
            fetch(
                0,
                {"video": {}},
                [{"format_id": "download", "url": "https://sf/mix.mp4"}],
            ),
            (b"mix", {"Referer": "https://www.tiktok.com/@u/video/123"}),
        )
        self.assertIsNone(fetch(0, {"video": {}}, [{"format_id": "play"}]))
        self.assertIsNone(fetch(500, {}, []))

    def test_restore_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            job = media.Job("https://www.tiktok.com/@u/video/123", None, directory)
            info = {"id": "123"}
            video = directory / "v.mp4"
            video.write_bytes(b"x")
            merged = directory / "merged.mp4"
            merged.write_bytes(b"y")

            def no_mix():
                return mock.patch.object(
                    media, "download_tiktok_mix", return_value=None
                )

            with no_mix():
                with mock.patch.object(media, "tiktok_music_url", return_value=None):
                    self.assertIs(media.restore_audio(video, job, info), video)

            with no_mix():
                with mock.patch.object(
                    media, "tiktok_music_url", return_value="https://sf/a.m4a"
                ):
                    with mock.patch.object(media, "download_file") as download_file:
                        with mock.patch.object(media, "mux_audio", return_value=merged):
                            self.assertIs(media.restore_audio(video, job, info), merged)
                    download_file.assert_called_once_with(
                        "https://sf/a.m4a", None, directory / "music.m4a"
                    )

            with no_mix():
                with mock.patch.object(
                    media, "tiktok_music_url", side_effect=RuntimeError("boom")
                ):
                    self.assertIs(media.restore_audio(video, job, info), video)

            with mock.patch.object(
                media, "download_tiktok_mix", return_value=directory / "mix.mp4"
            ):
                with mock.patch.object(media, "has_audio_stream", return_value=True):
                    with mock.patch.object(media, "tiktok_music_url") as music_url:
                        with mock.patch.object(media, "mux_audio", return_value=merged):
                            self.assertIs(media.restore_audio(video, job, info), merged)
                music_url.assert_not_called()

            with mock.patch.object(
                media, "download_tiktok_mix", return_value=directory / "mix.mp4"
            ):
                with mock.patch.object(media, "has_audio_stream", return_value=False):
                    with mock.patch.object(
                        media, "download_file", return_value=directory / "music.m4a"
                    ):
                        with mock.patch.object(
                            media, "tiktok_music_url", return_value="https://sf/a.m4a"
                        ):
                            with mock.patch.object(
                                media, "mux_audio", return_value=merged
                            ):
                                self.assertIs(
                                    media.restore_audio(video, job, info), merged
                                )

    def test_download_video(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video = directory / "123.mp4"
            video.write_bytes(b"x")
            job = media.Job("https://www.tiktok.com/@u/video/123", None, directory)
            info = {"id": "123", "ext": "mp4"}

            def run(has_audio):
                with mock.patch.object(media.yt_dlp, "YoutubeDL") as youtube_dl:
                    with mock.patch.object(
                        media, "has_audio_stream", return_value=has_audio
                    ):
                        with mock.patch.object(
                            media, "restore_audio", return_value=video
                        ) as restore:
                            ydl = youtube_dl.return_value.__enter__.return_value
                            ydl.extract_info.return_value = info
                            ydl.prepare_filename.return_value = str(video)
                            _, got_path = media.download_video(job)
                            return got_path, restore

            got_path, restore = run(True)
            self.assertEqual(got_path, video)
            restore.assert_not_called()
            got_path, restore = run(False)
            self.assertEqual(got_path, video)
            restore.assert_called_once()

            info_bad = {**info, "ext": "webm"}
            with mock.patch.object(media.yt_dlp, "YoutubeDL") as youtube_dl:
                ydl = youtube_dl.return_value.__enter__.return_value
                ydl.extract_info.return_value = info_bad
                ydl.prepare_filename.return_value = str(video)
                with self.assertRaises(ValueError):
                    media.download_video(job)

    def test_download_slideshow(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            job = media.Job("https://www.tiktok.com/@u/photo/123", None, directory)
            info = {
                "id": "123",
                "duration": 4,
                "formats": [{"vcodec": "none", "url": "https://sf/a.mp3"}],
            }
            output = directory / "slideshow.mp4"
            output.write_bytes(b"x")
            images = [directory / "1.jpg", directory / "2.jpg"]

            with mock.patch.object(
                slideshow, "download_file", return_value=directory / "audio.mp3"
            ) as download_file:
                with mock.patch.object(slideshow, "media_duration", return_value=4.0):
                    with mock.patch.object(
                        slideshow, "run_ffmpeg", return_value=output
                    ) as run_ffmpeg:
                        got_info, got_path = slideshow.download_slideshow(
                            info, images, job
                        )

            args = run_ffmpeg.call_args.args[0]
            self.assertNotIn("-stream_loop", args)
            self.assertEqual(got_path, output)
            self.assertEqual(got_info["ext"], "mp4")
            self.assertEqual(got_info["width"], config.SLIDESHOW_WIDTH)
            self.assertEqual(got_info["duration"], 4.0)
            download_file.assert_called_once_with(
                "https://sf/a.mp3", None, directory / "audio.mp3"
            )
            with self.assertRaises(ValueError):
                slideshow.download_slideshow({**info, "formats": []}, images, job)

            with mock.patch.object(
                slideshow, "download_file", return_value=directory / "audio.mp3"
            ):
                with mock.patch.object(slideshow, "media_duration", return_value=2.0):
                    with mock.patch.object(
                        slideshow, "run_ffmpeg", return_value=output
                    ) as run_ffmpeg:
                        slideshow.download_slideshow(info, images, job)
            args = run_ffmpeg.call_args.args[0]
            loop = args.index("-stream_loop")
            self.assertEqual(args[loop + 1], "-1")

    def test_resolve_video_flow(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    bot = SimpleNamespace(
                        send_video=AsyncMock(
                            return_value=SimpleNamespace(
                                video=SimpleNamespace(
                                    file_id="abc",
                                    width=1080,
                                    height=1920,
                                    duration=30,
                                )
                            )
                        )
                    )
                    service = video_service.VideoService(
                        bot, cache.FileIdCache(), config.ServiceConfig(None, 0)
                    )
                    url = "https://www.tiktok.com/@u/video/123"
                    metadata = {"id": "123", "title": "t", "media_type": "video"}
                    with (
                        mock.patch.object(
                            video_service, "extract_metadata", return_value=metadata
                        ) as extract_metadata,
                        mock.patch.object(
                            video_service,
                            "download_video",
                            return_value=(metadata, Path("/tmp/x.mp4")),
                        ),
                    ):
                        key, record = await service._resolve(url)
                        await service._resolve(url)
                    self.assertEqual(key, "tiktok:123")
                    self.assertEqual(record["file_id"], "abc")
                    self.assertEqual(extract_metadata.call_count, 1)
                    self.assertEqual(await service.cache.get("alias:" + url), key)

        asyncio.run(check())

    def test_resolve_photo_flow(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    bot = SimpleNamespace(
                        send_video=AsyncMock(
                            return_value=SimpleNamespace(
                                video=SimpleNamespace(
                                    file_id="abc",
                                    width=1080,
                                    height=1920,
                                    duration=30,
                                )
                            )
                        )
                    )
                    service = video_service.VideoService(
                        bot, cache.FileIdCache(), config.ServiceConfig(None, 0)
                    )
                    metadata = {
                        "id": "456",
                        "media_type": "photo",
                        "image_urls": [{"url": "u1"}, {"url": "u2"}],
                    }
                    with (
                        mock.patch.object(
                            video_service, "extract_metadata", return_value=metadata
                        ),
                        mock.patch.object(
                            video_service,
                            "download_images",
                            return_value=[Path("1.jpg"), Path("2.jpg")],
                        ) as download_images,
                        mock.patch.object(
                            video_service,
                            "download_slideshow",
                            return_value=(metadata, Path("/tmp/s.mp4")),
                        ),
                    ):
                        key, record = await service._resolve(
                            "https://www.tiktok.com/@u/photo/456"
                        )
                    self.assertEqual(key, "tiktok:456")
                    self.assertEqual(record["file_id"], "abc")
                    download_images.assert_called_once()

        asyncio.run(check())

    def test_service_close_cancels_inflight(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            release = asyncio.Event()

            async def hang(url):
                await release.wait()

            service._resolve = hang
            task = asyncio.create_task(
                service.result_for("https://www.tiktok.com/@u/video/1")
            )
            await asyncio.sleep(0)
            await service.close()
            self.assertTrue(task.cancelled())

        asyncio.run(check())

    def test_report_background_failure(self):
        async def check():
            cancelled = asyncio.create_task(asyncio.sleep(5))
            cancelled.cancel()
            await asyncio.sleep(0)  # deliver the cancellation
            handlers.report_background_failure(cancelled)

            async def boom():
                raise RuntimeError("x")

            with self.assertLogs("ttblow", level="ERROR") as captured:
                task = asyncio.create_task(boom())
                await asyncio.sleep(0)
                handlers.report_background_failure(task)
            self.assertIn("Timed-out video job failed", captured.output[0])

        asyncio.run(check())

    def test_inline_query_empty_text(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            calls = []

            async def answer(results, **kwargs):
                calls.append((results, kwargs))

            query = SimpleNamespace(
                query="", id="q1", from_user=SimpleNamespace(id=7), answer=answer
            )
            await handlers.inline_query(query, service)
            self.assertEqual(calls[0][0], [])
            self.assertIsNone(calls[0][1]["switch_pm_parameter"])

        asyncio.run(check())

    def test_inline_query_rate_limited(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.allow_user = AsyncMock(return_value=False)
            calls = []

            async def answer(results, **kwargs):
                calls.append((results, kwargs))

            query = SimpleNamespace(
                query="https://www.tiktok.com/@u/video/1",
                id="q1",
                from_user=SimpleNamespace(id=7),
                answer=answer,
            )
            await handlers.inline_query(query, service)
            self.assertEqual(calls[0][0], [])

        asyncio.run(check())

    def test_inline_query_success(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            record = {"file_id": "abc", "title": "t", "description": "d"}
            service.result_for = AsyncMock(return_value=("tiktok:123", record))
            calls = []

            async def answer(results, **kwargs):
                calls.append((results, kwargs))

            query = SimpleNamespace(
                query="https://www.tiktok.com/@u/video/123",
                id="q1",
                from_user=SimpleNamespace(id=7),
                answer=answer,
            )
            await handlers.inline_query(query, service)
            self.assertEqual(len(calls[0][0]), 1)
            self.assertEqual(calls[0][0][0].video_file_id, "abc")

        asyncio.run(check())

    def test_inline_query_timeout_moves_to_pm(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            release = asyncio.Event()

            async def hang(url):
                await release.wait()
                return "tiktok:123", {}

            service.result_for = hang
            service.inline_timeout = 0.05
            calls = []

            async def answer(results, **kwargs):
                calls.append((results, kwargs))

            query = SimpleNamespace(
                query="https://www.tiktok.com/@u/video/123",
                id="q1",
                from_user=SimpleNamespace(id=7),
                answer=answer,
            )
            await handlers.inline_query(query, service)
            self.assertEqual(calls[0][0], [])
            self.assertIsNotNone(calls[0][1]["switch_pm_parameter"])
            release.set()
            await asyncio.sleep(0.01)

        asyncio.run(check())

    def test_inline_query_answer_error_is_logged(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            service.result_for = AsyncMock(
                return_value=("tiktok:123", {"file_id": "abc"})
            )

            async def answer(**kwargs):
                raise RuntimeError("telegram down")

            query = SimpleNamespace(
                query="https://www.tiktok.com/@u/video/123",
                id="q1",
                from_user=SimpleNamespace(id=7),
                answer=answer,
            )
            await handlers.inline_query(query, service)  # must not raise

        asyncio.run(check())

    def test_private_start_success(self):
        async def check():
            edited = []

            async def edit_message_media(**kwargs):
                edited.append(kwargs)

            bot = SimpleNamespace(edit_message_media=edit_message_media)
            service = video_service.VideoService(
                bot, object(), config.ServiceConfig(None, 0)
            )
            task_id = service.register_pm_task(
                "https://www.tiktok.com/@u/video/123", 42
            )
            service.result_for = AsyncMock(
                return_value=("tiktok:123", {"file_id": "abc"})
            )
            placeholders = []

            async def answer(text):
                placeholder = SimpleNamespace(
                    message_id=1, edit_text=AsyncMock(), delete=AsyncMock()
                )
                placeholders.append(placeholder)
                return placeholder

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await handlers.private_start(message, command, service)
            self.assertEqual(len(edited), 1)
            self.assertEqual(edited[0]["media"].media, "abc")

        asyncio.run(check())

    def test_private_start_reports_result_failure(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            task_id = service.register_pm_task(
                "https://www.tiktok.com/@u/video/123", 42
            )
            service.result_for = AsyncMock(side_effect=RuntimeError("boom"))
            placeholders = []

            async def answer(text):
                placeholder = SimpleNamespace(
                    message_id=1, edit_text=AsyncMock(), delete=AsyncMock()
                )
                placeholders.append(placeholder)
                return placeholder

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await handlers.private_start(message, command, service)
            placeholders[0].edit_text.assert_called_once_with(
                "❌ Не удалось обработать видео. Попробуйте ещё раз."
            )

        asyncio.run(check())

    def test_private_start_falls_back_on_edit_failure(self):
        async def check():
            async def edit_message_media(**kwargs):
                raise RuntimeError("no")

            bot = SimpleNamespace(
                edit_message_media=edit_message_media, send_video=AsyncMock()
            )
            service = video_service.VideoService(
                bot, object(), config.ServiceConfig(None, 0)
            )
            task_id = service.register_pm_task(
                "https://www.tiktok.com/@u/video/123", 42
            )
            service.result_for = AsyncMock(
                return_value=("tiktok:123", {"file_id": "abc"})
            )
            placeholders = []

            async def answer(text):
                placeholder = SimpleNamespace(
                    message_id=1, edit_text=AsyncMock(), delete=AsyncMock()
                )
                placeholders.append(placeholder)
                return placeholder

            message = SimpleNamespace(
                chat=SimpleNamespace(id=7, type="private"),
                from_user=SimpleNamespace(id=42),
                answer=answer,
            )
            command = SimpleNamespace(args=task_id)
            await handlers.private_start(message, command, service)
            bot.send_video.assert_called_once()
            placeholders[0].delete.assert_called_once()

        asyncio.run(check())

    def test_resolve_hits_existing_record(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    service = video_service.VideoService(
                        object(), cache.FileIdCache(), config.ServiceConfig(None, 0)
                    )
                    url = "https://www.tiktok.com/@u/video/123"
                    record = {"file_id": "abc", "type": "video"}
                    await service.cache.set("tiktok:123", record)
                    with mock.patch.object(
                        video_service, "extract_metadata", return_value={"id": "123"}
                    ) as extract_metadata:
                        key, got = await service._resolve(url)
                    self.assertEqual(key, "tiktok:123")
                    self.assertEqual(got, record)
                    self.assertEqual(await service.cache.get("alias:" + url), key)
                    extract_metadata.assert_called_once()

        asyncio.run(check())

    def test_resolve_evicts_stale_alias(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                with env("DISK_CACHE_DIR", str(Path(directory) / "cache")):
                    bot = SimpleNamespace(
                        send_video=AsyncMock(
                            return_value=SimpleNamespace(
                                video=SimpleNamespace(
                                    file_id="abc",
                                    width=1080,
                                    height=1920,
                                    duration=30,
                                )
                            )
                        )
                    )
                    service = video_service.VideoService(
                        bot, cache.FileIdCache(), config.ServiceConfig(None, 0)
                    )
                    url = "https://www.tiktok.com/@u/video/123"
                    await service.cache.set("tiktok:old", {"type": "photo"})
                    await service.cache.set("alias:" + url, "tiktok:old")
                    with (
                        mock.patch.object(
                            video_service,
                            "extract_metadata",
                            return_value={"id": "123"},
                        ),
                        mock.patch.object(
                            video_service,
                            "download_video",
                            return_value=({"id": "123"}, Path("/tmp/x.mp4")),
                        ),
                    ):
                        key, _ = await service._resolve(url)
                    self.assertEqual(key, "tiktok:123")
                    self.assertEqual(
                        await service.cache.get("alias:" + url), "tiktok:123"
                    )
                    self.assertIsNone(await service.cache.get("tiktok:old"))

        asyncio.run(check())

    def test_main_starts_polling(self):
        async def check():
            cache = SimpleNamespace(close=AsyncMock())
            session = SimpleNamespace(close=AsyncMock())
            bot = SimpleNamespace(session=session)
            dispatcher = SimpleNamespace(start_polling=AsyncMock())
            with env("TELEGRAM_BOT_TOKEN", "token"), env("TELEGRAM_CACHE_CHAT_ID", "1"):
                with mock.patch.object(entry, "Bot", return_value=bot):
                    with mock.patch.object(entry, "AiohttpSession"):
                        with mock.patch.object(
                            entry, "FileIdCache", return_value=cache
                        ):
                            with mock.patch.object(
                                entry, "make_dispatcher", return_value=dispatcher
                            ) as make_dispatcher:
                                await entry.main()
            dispatcher.start_polling.assert_called_once()
            cache.close.assert_called_once()
            session.close.assert_called_once()
            self.assertIs(make_dispatcher.call_args.args[0].bot, bot)

        asyncio.run(check())

    def test_private_start_ignores_non_private_chat(self):
        async def check():
            service = video_service.VideoService(
                object(), object(), config.ServiceConfig(None, 0)
            )
            calls = []

            async def answer(text):
                calls.append(text)

            message = SimpleNamespace(
                chat=SimpleNamespace(type="group"),
                from_user=SimpleNamespace(id=42),
                answer=answer,
            )
            command = SimpleNamespace(args="anything")
            await handlers.private_start(message, command, service)
            self.assertEqual(calls, [])

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
