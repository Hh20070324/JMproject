import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import jmcomic

from jm_downloader.settings import AppPaths, AppSettings
from jm_downloader.task_store import StoredTask, TaskStore


V29_FEATURE_CONTRACTS = {
    "async_engine": "task_bound_and_sync_fallback",
    "api_route": "api_only_and_task_bound",
    "package_format": "pdf_cbz_or_images",
    "chapter_selection": "unlimited_ui_split_into_ten",
    "search_history": "dpapi_recent_fifty",
    "remember_credentials": "opt_in_dpapi_no_auto_submit",
    "library": "multi_delete_title_search_download_time_sort",
}


class _ProbeAsyncDownloader(jmcomic.JmAsyncDownloader):
    def __init__(self, option, *, stop_event=None):
        super().__init__(
            option,
            image_concurrency=2,
            photo_concurrency=1,
            decode_worker=1,
        )
        self.stop_event = stop_event or asyncio.Event()
        self.active = 0
        self.maximum_active = 0
        self.started = []
        self.published = []

    async def _download_single_image(self, image):
        if self.stop_event.is_set():
            return
        async with self._image_semaphore:
            if self.stop_event.is_set():
                return
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.started.append(image.download_url)
            try:
                await asyncio.sleep(0)
                if image.download_url == "failure":
                    raise RuntimeError("probe failure")
                if self.stop_event.is_set():
                    return
                self.published.append(image.download_url)
            finally:
                self.active -= 1


class V29AsyncEngineProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.option = SimpleNamespace(
            download=SimpleNamespace(
                threading=SimpleNamespace(image=7, photo=3),
            ),
        )
        self.downloader = _ProbeAsyncDownloader(self.option)

    async def asyncTearDown(self):
        self.downloader.shutdown()

    def test_pinned_async_surface_has_project_override_point(self):
        self.assertTrue(
            inspect.iscoroutinefunction(
                jmcomic.JmAsyncDownloader._download_single_image
            )
        )
        self.assertTrue(
            inspect.iscoroutinefunction(jmcomic.download_album_async)
        )
        signature = inspect.signature(jmcomic.download_album_async)
        self.assertIn("downloader", signature.parameters)
        self.assertIn("check_exception", signature.parameters)

    async def test_custom_single_image_hook_keeps_bounded_concurrency(self):
        images = [
            SimpleNamespace(download_url=f"image-{index}")
            for index in range(6)
        ]

        await asyncio.gather(
            *(self.downloader._safe_download_image(image) for image in images)
        )

        self.assertEqual(self.downloader.maximum_active, 2)
        self.assertCountEqual(
            self.downloader.published,
            [image.download_url for image in images],
        )

    async def test_cancel_checkpoints_prevent_request_and_publish(self):
        self.downloader.stop_event.set()
        image = SimpleNamespace(download_url="cancelled")

        await self.downloader._safe_download_image(image)

        self.assertEqual(self.downloader.started, [])
        self.assertEqual(self.downloader.published, [])

    async def test_image_failure_is_aggregated_by_upstream_wrapper(self):
        failed = SimpleNamespace(download_url="failure")

        await self.downloader._safe_download_image(failed)

        self.assertEqual(len(self.downloader.download_failed_image), 1)
        image, error = self.downloader.download_failed_image[0]
        self.assertIs(image, failed)
        self.assertIsInstance(error, RuntimeError)
        with self.assertRaises(jmcomic.PartialDownloadFailedException):
            self.downloader.raise_if_has_exception()

    async def test_async_context_closes_client_and_decode_pool_on_error(self):
        class FakeClient:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        client = FakeClient()
        self.downloader.client = client

        await self.downloader.__aexit__(
            RuntimeError,
            RuntimeError("probe"),
            None,
        )

        self.assertTrue(client.closed)
        self.assertIsNone(self.downloader.client)


class V29MigrationBaselineTests(unittest.TestCase):
    def test_seven_feature_tracks_are_explicit_and_non_overlapping(self):
        self.assertEqual(
            set(V29_FEATURE_CONTRACTS),
            {
                "async_engine",
                "api_route",
                "package_format",
                "chapter_selection",
                "search_history",
                "remember_credentials",
                "library",
            },
        )

    def test_v28_settings_sample_keeps_v28_defaults(self):
        settings = AppSettings.from_dict(
            {
                "schema_version": 1,
                "paths": {"pictures": "Pictures", "pdfs": "PDFs"},
                "download": {
                    "max_concurrent_tasks": 2,
                    "image_concurrency": 16,
                    "multi_chapter_download_behavior": "parallel",
                },
            }
        )

        self.assertEqual(settings.image_concurrency, 16)
        self.assertEqual(settings.multi_chapter_download_behavior, "parallel")

    def test_v28_task_sample_preserves_chapter_selection_and_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = AppPaths(root=root)
            paths.tasks_file.write_text(
                """{
  "schema_version": 2,
  "tasks": [
    {
      "id": "phase0-sample",
      "album_id": "123",
      "title": "sample",
      "status": "paused",
      "progress": 40,
      "chapter": "chapter",
      "page": "page",
      "error": null,
      "selected_chapter_ids": ["301", "302"],
      "paths": {"pictures": "Pictures", "pdfs": "PDFs"}
    }
  ]
}
""",
                encoding="utf-8",
            )

            records = TaskStore(paths).load()

        self.assertEqual(len(records), 1)
        self.assertIsInstance(records[0], StoredTask)
        self.assertEqual(records[0].selected_chapter_ids, ("301", "302"))
        self.assertEqual(records[0].pictures_directory, "Pictures")
        self.assertEqual(records[0].pdf_directory, "PDFs")
