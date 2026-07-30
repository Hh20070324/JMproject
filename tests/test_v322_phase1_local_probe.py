import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from jm_downloader.library import (
    ChapterManifestStore,
    LibraryError,
    LibraryService,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    ChapterSnapshot,
    LocalReadProbeSnapshot,
    LocalReadProbeState,
)
from jm_downloader.qt.controllers import LibraryController
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskManager


def manifest(*chapters: ChapterManifestEntry) -> ChapterManifest:
    return ChapterManifest(
        version=3,
        album_id="123",
        album_title="测试漫画",
        album_dir_name="测试漫画",
        chapters=chapters,
    )


def chapter(
    photo_id: str,
    index: int,
    directory: str,
) -> ChapterManifestEntry:
    return ChapterManifestEntry(
        photo_id=photo_id,
        index=index,
        title=f"第 {index} 章",
        dir_name=directory,
        page_count=1,
        image_format="jpg",
        package_format="images",
    )


class V322LocalReadProbeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.service = LibraryService(self.paths)

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_manifest_returns_normal_absence(self):
        result = self.service.probe_local_read("123")

        self.assertEqual(
            result,
            LocalReadProbeSnapshot("123", LocalReadProbeState.ABSENT),
        )

    def test_mixed_album_returns_only_complete_chapters_in_manifest_order(self):
        first = chapter("301", 1, "第1章")
        second = chapter("302", 2, "第2章")
        image = (
            self.paths.pictures
            / "123"
            / "测试漫画"
            / first.dir_name
            / "1.jpg"
        )
        image.parent.mkdir(parents=True)
        Image.new("RGB", (12, 12), "white").save(image, format="JPEG")
        ChapterManifestStore(self.paths).replace_exact(manifest(first, second))

        result = self.service.probe_local_read("123")

        self.assertEqual(result.state, LocalReadProbeState.READY)
        self.assertEqual(
            result.chapters,
            (ChapterSnapshot("301", 1, "第 1 章", True),),
        )

    def test_existing_manifest_without_complete_chapter_is_unavailable(self):
        ChapterManifestStore(self.paths).replace_exact(
            manifest(chapter("301", 1, "第1章"))
        )

        result = self.service.probe_local_read("123")

        self.assertEqual(result.state, LocalReadProbeState.UNAVAILABLE)
        self.assertEqual(result.chapters, ())

    def test_per_chapter_check_error_is_not_downgraded_to_unavailable(self):
        ChapterManifestStore(self.paths).replace_exact(
            manifest(chapter("301", 1, "第1章"))
        )
        with patch.object(
            self.service,
            "_check_chapter",
            side_effect=OSError("private path detail"),
        ):
            with self.assertRaisesRegex(LibraryError, "本地章节检查失败"):
                self.service.probe_local_read("123")


class _PassiveWorker:
    def __init__(self, album_id, **_callbacks):
        self.album_id = album_id

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, _timeout):
        return True


class _ProbeLibrary:
    def __init__(self):
        self.thread = None

    def probe_local_read(self, album_id):
        self.thread = threading.current_thread()
        return LocalReadProbeSnapshot(
            album_id,
            LocalReadProbeState.READY,
            (ChapterSnapshot("301", 1, "第 1 章", True),),
        )


class V322LocalReadProbeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-local-probe-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=_PassiveWorker,
        )
        self.library = _ProbeLibrary()
        self.controller = LibraryController(
            self.manager,
            self.library,
            thread_pool=QThreadPool(),
            event_interval_ms=10,
            reconcile_interval_ms=50,
        )

    def tearDown(self):
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

    def test_probe_runs_in_background_without_mutation_busy_state(self):
        completed = []
        self.controller.request_completed.connect(
            lambda *values: completed.append(values)
        )

        request_id = self.controller.probe_local_read("123")

        self.assertIsInstance(request_id, int)
        self.assertEqual(self.controller.busy_album_ids(), frozenset())
        self.assertTrue(self._wait_until(lambda: bool(completed)))
        self.assertEqual(
            completed[0][:3],
            (request_id, "probe_local_read", "123"),
        )
        self.assertEqual(
            completed[0][3].state,
            LocalReadProbeState.READY,
        )
        self.assertIsNot(self.library.thread, threading.main_thread())
        self.assertFalse(self.manager.is_library_operation_active("123"))
        self.assertEqual(self.controller.busy_album_ids(), frozenset())


if __name__ == "__main__":
    unittest.main()
