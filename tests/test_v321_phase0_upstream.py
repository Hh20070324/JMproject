import asyncio
import inspect
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import jmcomic
from jmcomic.jm_async_client import AsyncJmApiClient

from jm_downloader import downloader
from jm_downloader.models import TaskConfig
from jm_downloader.settings import AppPaths


ROOT = Path(__file__).resolve().parent.parent


class V321UpstreamContractTests(unittest.TestCase):
    def test_pinned_version_and_async_query_surface(self):
        self.assertEqual(version("jmcomic"), "2.7.2")
        self.assertEqual(jmcomic.__version__, "2.7.2")

        factory = inspect.signature(jmcomic.JmOption.new_jm_async_client)
        self.assertIn("domain_list", factory.parameters)
        for method_name in (
            "setup",
            "close",
            "search_site",
            "get_album_detail",
            "login",
            "favorite_folder",
        ):
            self.assertTrue(
                inspect.iscoroutinefunction(
                    getattr(AsyncJmApiClient, method_name)
                ),
                method_name,
            )

    def test_async_downloader_private_hooks_remain_available(self):
        active = jmcomic.JmAsyncDownloader(
            SimpleNamespace(
                download=SimpleNamespace(
                    threading=SimpleNamespace(image=2, photo=1)
                )
            )
        )
        for attribute in (
            "_photo_semaphore",
            "_image_semaphore",
            "_decode_pool",
            "_safe_download_photo",
            "_safe_download_image",
        ):
            self.assertTrue(
                hasattr(active, attribute),
                attribute,
            )
        active.shutdown()

    def test_license_and_build_inventory_match_the_pin(self):
        self.assertTrue(
            (
                ROOT
                / "LICENSES"
                / "JMComic-Crawler-Python-2.7.2.txt"
            ).is_file()
        )
        self.assertFalse(
            (
                ROOT
                / "LICENSES"
                / "JMComic-Crawler-Python-2.7.1.txt"
            ).exists()
        )
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        build_script = (ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("jmcomic==2.7.2", requirements)
        self.assertIn("JMComic-Crawler-Python-2.7.2.txt", build_script)
        self.assertNotIn("JMComic-Crawler-Python-2.7.1.txt", build_script)


class V321AsyncDownloaderCleanupTests(unittest.TestCase):
    def test_project_downloader_cleans_setup_failure(self):
        class SetupFailure(RuntimeError):
            pass

        class FakeClient:
            def __init__(self):
                self.closed = False

            async def setup(self):
                raise SetupFailure("private endpoint marker")

            async def close(self):
                self.closed = True

        client = FakeClient()
        captured = {}

        async def fake_download(_album_id, option, *, downloader, **_kwargs):
            active = downloader(option)
            captured["active"] = active
            await active.__aenter__()

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            worker = downloader.DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(download_engine="async"),
                selected_chapter_ids=("301",),
            )
            option = SimpleNamespace(
                download=SimpleNamespace(
                    threading=SimpleNamespace(image=2, photo=1)
                ),
                new_jm_async_client=lambda **_kwargs: client,
                dir_rule=SimpleNamespace(base_dir=""),
            )
            with (
                patch.object(worker, "_make_option", return_value=option),
                patch.object(
                    downloader.jmcomic,
                    "download_album_async",
                    side_effect=fake_download,
                ),
            ):
                worker.run()

        self.assertTrue(client.closed)
        self.assertIsNone(captured["active"].client)
        self.assertTrue(captured["active"]._decode_pool._shutdown)
