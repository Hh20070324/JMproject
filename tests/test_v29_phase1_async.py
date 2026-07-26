import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from jm_downloader import downloader
from jm_downloader.models import TaskConfig
from jm_downloader.settings import AppPaths, AppSettings
from jm_downloader.task_store import TaskStore
from jm_downloader.tasks import TaskManager


class _CapturingWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.task_config = kwargs["task_config"]
        self.callbacks = kwargs
        self.__class__.instances.append(self)

    def start(self):
        return None

    def stop(self):
        return None

    def wait(self, _timeout=None):
        return True


class V29TaskConfigTests(unittest.TestCase):
    def setUp(self):
        _CapturingWorker.instances.clear()

    def test_settings_default_to_async_and_round_trip(self):
        settings = AppSettings()
        restored = AppSettings.from_dict(settings.to_dict())

        self.assertEqual(restored.download_engine, "async")
        self.assertEqual(restored.task_config(), TaskConfig())

    def test_new_task_locks_config_while_later_task_uses_new_setting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            first = TaskConfig(download_engine="async", image_concurrency=5)
            second = TaskConfig(download_engine="sync", image_concurrency=9)
            manager = TaskManager(
                paths=paths,
                max_concurrent=1,
                worker_factory=_CapturingWorker,
                new_task_config=first,
            )
            try:
                first_task = manager.add("101", selected_chapter_ids=("1",))
                manager.set_new_task_config(second)
                second_task = manager.add("202", selected_chapter_ids=("2",))

                self.assertEqual(first_task.config, first)
                self.assertEqual(second_task.config, second)
                self.assertEqual(_CapturingWorker.instances[0].task_config, first)
            finally:
                manager.shutdown(timeout=2)

    def test_v2_task_migrates_to_sync_with_current_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            paths.tasks_file.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "tasks": [
                            {
                                "id": "legacy01",
                                "album_id": "123",
                                "title": None,
                                "status": "paused",
                                "progress": 1,
                                "chapter": "",
                                "page": "",
                                "error": None,
                                "selected_chapter_ids": ["301"],
                                "paths": {
                                    "pictures": "Pictures",
                                    "pdfs": "PDFs",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            legacy = TaskConfig(
                download_engine="sync",
                image_concurrency=23,
            )
            store = TaskStore(paths, legacy_task_config=legacy)

            records = store.load()

        self.assertEqual(records[0].config, legacy)


class V29AsyncImagePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_image_uses_part_validation_and_atomic_publish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            task_config = TaskConfig(
                download_engine="async",
                image_concurrency=2,
            )
            worker = downloader.DownloadWorker(
                "123",
                paths=paths,
                task_config=task_config,
                selected_chapter_ids=("301",),
            )
            final_path = paths.pictures / "123" / "title" / "1.jpg"
            option = Mock()
            option.decide_image_filepath.return_value = str(final_path)
            option.decide_download_cache.return_value = False
            option.decide_download_image_decode.return_value = False
            active = SimpleNamespace(
                option=option,
                client=SimpleNamespace(
                    get_jm_image=AsyncMock(
                        return_value=SimpleNamespace(
                            content=self._jpeg_bytes()
                        )
                    )
                ),
                before_image=AsyncMock(),
                after_image=AsyncMock(),
                _image_semaphore=asyncio.Semaphore(2),
                _decode_pool=None,
                _save_raw=lambda value, target, _convert: Path(
                    target
                ).write_bytes(value),
            )
            image = SimpleNamespace(
                download_url="https://example.invalid/1.jpg",
                scramble_id=None,
                aid="123",
                img_file_name="1.jpg",
                skip=False,
                from_photo=None,
            )

            await worker._download_image_async(active, image)

            self.assertTrue(final_path.is_file())
            self.assertTrue(worker._is_valid_image(final_path))
            self.assertEqual(list(final_path.parent.glob("*.part-*")), [])
            active.before_image.assert_awaited_once()
            active.after_image.assert_awaited_once()

    async def test_async_cancel_before_request_leaves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            worker = downloader.DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(download_engine="async"),
                selected_chapter_ids=("301",),
            )
            worker.stop()
            client = SimpleNamespace(get_jm_image=AsyncMock())
            active = SimpleNamespace(
                option=Mock(),
                client=client,
            )
            image = SimpleNamespace()

            with self.assertRaises(downloader.DownloadStopped):
                await worker._download_image_async(active, image)

            client.get_jm_image.assert_not_awaited()

    @staticmethod
    def _jpeg_bytes():
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "green").save(buffer, format="JPEG")
        return buffer.getvalue()
