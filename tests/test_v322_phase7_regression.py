import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from jm_downloader.library import ChapterManifestStore, LibraryService
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    ReaderContentMode,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.qt.controllers import LibraryController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskManager


class _PassiveWorker:
    def __init__(self, album_id, **_callbacks):
        self.album_id = album_id

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, _timeout):
        return True


class V322EndToEndRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-phase7-regression-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=_PassiveWorker,
        )
        self.service = LibraryService(self.paths)
        self.controller = LibraryController(
            self.manager,
            self.service,
            thread_pool=QThreadPool(),
            event_interval_ms=10,
            reconcile_interval_ms=50,
        )
        self.window = MainWindow(
            ThemeManager("light"),
            library_controller=self.controller,
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.controller.request_completed.connect(
            self.window._on_local_read_probe_completed
        )
        self.controller.request_failed.connect(
            self.window._on_local_read_probe_failed
        )

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.controller.shutdown(3)
        self.manager.shutdown(1)
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def _wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def _write_manifest(self, album_id, *, with_image):
        title = f"漫画 {album_id}"
        directory = (
            self.paths.pictures
            / str(album_id)
            / title
            / "第1章"
        )
        if with_image:
            directory.mkdir(parents=True)
            Image.new("RGB", (16, 16), "white").save(
                directory / "1.jpg",
                format="JPEG",
            )
        ChapterManifestStore(self.paths).replace_exact(
            ChapterManifest(
                version=3,
                album_id=str(album_id),
                album_title=title,
                album_dir_name=title,
                chapters=(
                    ChapterManifestEntry(
                        photo_id=f"{album_id}01",
                        index=1,
                        title="第 1 章",
                        dir_name="第1章",
                        page_count=1,
                        image_format="jpg",
                        package_format="images",
                    ),
                ),
            )
        )
        return (
            self.paths.pictures
            / str(album_id)
            / ".jm-chapters.json"
        )

    def test_real_background_probe_opens_local_without_writing_manifest(self):
        manifest_path = self._write_manifest("123", with_image=True)
        before = manifest_path.read_bytes()
        snapshot = SearchResultSnapshot("123", "漫画 123")

        with patch.object(
            self.window,
            "_start_reader_session",
        ) as start, patch.object(
            self.window,
            "_open_reader",
        ) as online:
            self.window._request_local_first_reader(
                snapshot,
                ReaderSource.EXACT_SEARCH,
            )
            self.assertTrue(
                self._wait_until(lambda: start.call_count == 1)
            )

        online.assert_not_called()
        self.assertEqual(
            start.call_args.kwargs["content_mode"],
            ReaderContentMode.LOCAL,
        )
        self.assertEqual(
            start.call_args.kwargs["source"],
            ReaderSource.EXACT_SEARCH,
        )
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_real_absence_and_unavailable_results_take_different_routes(self):
        absent = SearchResultSnapshot("999", "未下载漫画")
        with patch.object(
            self.window,
            "_open_reader",
        ) as online, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._request_local_first_reader(
                absent,
                ReaderSource.FAVORITES,
            )
            self.assertTrue(
                self._wait_until(lambda: online.call_count == 1)
            )
            fallback.assert_not_called()

            self._write_manifest("124", with_image=False)
            unavailable = SearchResultSnapshot("124", "漫画 124")
            self.window._request_local_first_reader(
                unavailable,
                ReaderSource.SEARCH,
            )
            self.assertTrue(
                self._wait_until(lambda: fallback.call_count == 1)
            )

        online.assert_called_once_with(
            absent,
            ReaderSource.FAVORITES,
        )
        self.assertEqual(
            fallback.call_args.args[0],
            unavailable,
        )
        self.assertIn(
            "没有图片完整的章节",
            fallback.call_args.args[2],
        )

    def test_absent_probe_does_not_recreate_removed_output_directories(self):
        shutil.rmtree(self.paths.pictures)
        shutil.rmtree(self.paths.pdfs)

        result = self.service.probe_local_read("888")

        self.assertEqual(result.state.value, "absent")
        self.assertFalse(self.paths.pictures.exists())
        self.assertFalse(self.paths.pdfs.exists())


if __name__ == "__main__":
    unittest.main()
