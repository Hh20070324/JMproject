import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from PIL import Image

from jm_downloader.credentials import CredentialStore
from jm_downloader.downloader import DownloadWorker
from jm_downloader.models import TaskConfig, TaskStatus
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.settings_controller import (
    SettingsController,
)
from jm_downloader.search_history import SearchHistoryStore
from jm_downloader.settings import AppPaths, AppSettings
from jm_downloader.task_store import StoredTask, TaskStore
from jm_downloader.tasks import TaskManager


FIXED_ROUTE = "www.cdnhjk.net"


class ReliabilityProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"reliability\0" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix = b"reliability\0"
        if not ciphertext.startswith(prefix):
            raise ValueError("wrong user or corrupt data")
        return ciphertext[len(prefix) :][::-1]


class CapturingWorker:
    instances = []

    def __init__(self, album_id, **callbacks):
        self.album_id = album_id
        self.task_config = callbacks["task_config"]
        self.__class__.instances.append(self)

    def start(self):
        return None

    def stop(self):
        return None

    def wait(self, _timeout=None):
        return True


class FakeSettingsStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.settings = AppSettings(api_route=FIXED_ROUTE)

    def load(self):
        return self.settings


class Phase8ReliabilityTests(unittest.TestCase):
    def setUp(self):
        CapturingWorker.instances.clear()

    def test_restored_task_keeps_full_config_after_global_switch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            original = TaskConfig(
                download_engine="async",
                api_route=FIXED_ROUTE,
                package_format="cbz",
                image_format="png",
                image_concurrency=7,
                multi_chapter_download_behavior="queued",
            )
            store = TaskStore(paths)
            store.save(
                (
                    StoredTask(
                        id="persisted-1",
                        album_id="123",
                        title="恢复任务",
                        status=TaskStatus.PAUSED,
                        progress=35,
                        chapter="第二章",
                        page="3/10",
                        error=None,
                        pictures_directory="Pictures",
                        pdf_directory="PDFs",
                        selected_chapter_ids=("301", "302"),
                        force_redownload_chapter_ids=("302",),
                        config=original,
                    ),
                )
            )
            self.assertTrue(store.close(timeout=2))

            manager = TaskManager(
                paths=paths,
                max_concurrent=1,
                worker_factory=CapturingWorker,
                task_store=TaskStore(paths),
                new_task_config=TaskConfig(
                    download_engine="sync",
                    api_route="auto",
                    package_format="pdf",
                    image_format="jpg",
                    image_concurrency=20,
                ),
            )
            try:
                restored = manager.list_tasks()[0]
                self.assertEqual(restored.config, original)
                self.assertEqual(
                    restored.force_redownload_chapter_ids,
                    ("302",),
                )

                manager.resume(restored.id)

                self.assertEqual(len(CapturingWorker.instances), 1)
                self.assertEqual(
                    CapturingWorker.instances[0].task_config,
                    original,
                )
            finally:
                self.assertTrue(manager.shutdown(timeout=2))

    def test_fixed_route_failures_point_back_to_auto_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))

            def fail_probe(_path, _route):
                raise TimeoutError("private network details")

            controller = SettingsController(
                FakeSettingsStore(paths),
                route_probe=fail_probe,
            )
            failures = []
            controller.route_test_failed.connect(
                lambda route, message: failures.append((route, message))
            )
            with controller._route_test_lock:
                controller._route_test_generation = 1

            controller._run_route_test(1, FIXED_ROUTE)

            self.assertEqual(failures[0][0], FIXED_ROUTE)
            self.assertIn("切回“自动选择”", failures[0][1])
            self.assertNotIn("private network details", failures[0][1])

            fixed_worker = DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(api_route=FIXED_ROUTE),
                selected_chapter_ids=("301",),
            )
            auto_worker = DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(api_route="auto"),
                selected_chapter_ids=("301",),
            )
            self.assertIn(
                "切回“自动选择”",
                fixed_worker._public_error_message(TimeoutError()),
            )
            self.assertNotIn(
                "切回",
                auto_worker._public_error_message(TimeoutError()),
            )

    def test_oversize_protected_files_are_backed_up_without_parsing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            protector = ReliabilityProtector()

            credential_protected = ProtectedStore.credentials(
                paths,
                protector,
            )
            credentials = CredentialStore(credential_protected)
            paths.credentials_file.write_bytes(
                b"x" * (credential_protected.max_file_bytes + 1)
            )

            self.assertIsNone(credentials.load())
            self.assertIsNotNone(credentials.last_recovery_backup)
            self.assertFalse(paths.credentials_file.exists())

            history_protected = ProtectedStore.search_history(
                paths,
                protector,
            )
            history = SearchHistoryStore(history_protected)
            paths.search_history_file.write_bytes(
                b"x" * (history_protected.max_file_bytes + 1)
            )

            self.assertEqual(history.load(), ())
            self.assertIsNotNone(history.last_recovery_backup)
            self.assertFalse(paths.search_history_file.exists())


class Phase8FormatInterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_format_conversion_keeps_old_image_and_no_part(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            final_path = paths.pictures / "123" / "漫画" / "第1章" / "1.png"
            final_path.parent.mkdir(parents=True)
            Image.new("RGB", (2, 2), "green").save(final_path)
            original = final_path.read_bytes()
            worker = DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(
                    download_engine="async",
                    image_format="png",
                ),
                selected_chapter_ids=("301",),
            )

            def fail_conversion(content, target, _convert):
                Path(target).write_bytes(content[:8])
                raise RuntimeError("conversion interrupted")

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
                _image_semaphore=asyncio.Semaphore(1),
                _decode_pool=None,
                _save_raw=fail_conversion,
            )
            image = SimpleNamespace(
                download_url="https://example.invalid/1.jpg",
                scramble_id=None,
                aid="123",
                img_file_name="1.jpg",
                skip=False,
                from_photo=None,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "conversion interrupted",
            ):
                await worker._download_image_async(active, image)

            self.assertEqual(final_path.read_bytes(), original)
            self.assertEqual(
                tuple(final_path.parent.glob("*.part-*")),
                (),
            )
            active.after_image.assert_not_awaited()

    @staticmethod
    def _jpeg_bytes() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(buffer, format="JPEG")
        return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
