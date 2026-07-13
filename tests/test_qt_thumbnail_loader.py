import os
from pathlib import Path
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QEventLoop, QSize, QThread, QThreadPool, QTimer, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from jm_downloader.qt.widgets import ThumbnailLoader


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.events = []
        self.receiving_thread = None

    @Slot(str, int, QImage)
    def receive(self, task_id: str, revision: int, image: QImage) -> None:
        self.events.append((task_id, revision, image))
        self.receiving_thread = QThread.currentThread()


class _DeferredThreadPool:
    def __init__(self):
        self.runnables = []

    def start(self, runnable):
        self.runnables.append(runnable)


class ThumbnailLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["thumbnail-tests"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.thread_pool = QThreadPool()
        self.loader = ThumbnailLoader(thread_pool=self.thread_pool)
        self.receiver = _Receiver()
        self.loader.thumbnail_ready.connect(self.receiver.receive)

    def tearDown(self):
        self.thread_pool.waitForDone(3000)
        self.app.processEvents()
        self.loader.deleteLater()
        self.receiver.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_loads_scaled_image_and_delivers_on_main_thread(self):
        image_path = Path(self.temp_dir.name) / "cover.png"
        source = QImage(200, 100, QImage.Format.Format_RGB32)
        source.fill(0xFF247A52)
        self.assertTrue(source.save(str(image_path)))

        self.loader.request("task-1", 3, image_path, QSize(64, 64))
        self.assertTrue(self._wait_for_events(1))

        task_id, revision, result = self.receiver.events[0]
        self.assertEqual((task_id, revision), ("task-1", 3))
        self.assertFalse(result.isNull())
        self.assertEqual(result.size(), QSize(64, 32))
        self.assertEqual(self.receiver.receiving_thread, self.app.thread())

    def test_missing_image_emits_empty_image(self):
        missing = Path(self.temp_dir.name) / "missing.jpg"

        self.loader.request("missing", 1, missing, QSize(80, 80))
        self.assertTrue(self._wait_for_events(1))

        task_id, revision, result = self.receiver.events[0]
        self.assertEqual((task_id, revision), ("missing", 1))
        self.assertTrue(result.isNull())

    def test_suppresses_inflight_duplicate_and_reuses_cache(self):
        image_path = Path(self.temp_dir.name) / "cover.png"
        source = QImage(120, 180, QImage.Format.Format_RGB32)
        source.fill(0xFF404040)
        self.assertTrue(source.save(str(image_path)))

        self.loader.request("task-2", 1, image_path, QSize(60, 60))
        self.loader.request("task-2", 1, image_path, QSize(60, 60))
        self.assertTrue(self._wait_for_events(1))
        self.assertEqual(len(self.receiver.events), 1)

        self.loader.request("task-2", 1, image_path, QSize(60, 60))
        self.assertTrue(self._wait_for_events(2))
        self.assertEqual(self.receiver.events[1][2].size(), QSize(40, 60))

    def test_clear_task_suppresses_old_result_and_allows_new_generation(self):
        image_path = Path(self.temp_dir.name) / "cover.png"
        source = QImage(80, 120, QImage.Format.Format_RGB32)
        source.fill(0xFF206080)
        self.assertTrue(source.save(str(image_path)))
        deferred_pool = _DeferredThreadPool()
        loader = ThumbnailLoader(thread_pool=deferred_pool)
        loader.thumbnail_ready.connect(self.receiver.receive)

        loader.request("reused-task", 1, image_path, QSize(40, 40))
        loader.clear_task("reused-task")
        deferred_pool.runnables[0].run()
        self.app.processEvents()
        self.assertEqual(self.receiver.events, [])

        loader.request("reused-task", 2, image_path, QSize(40, 40))
        deferred_pool.runnables[1].run()
        self.assertTrue(self._wait_for_events(1))
        self.assertEqual(self.receiver.events[0][:2], ("reused-task", 2))
        loader.deleteLater()

    def _wait_for_events(self, count: int, timeout_ms: int = 3000) -> bool:
        if len(self.receiver.events) >= count:
            return True

        loop = QEventLoop()

        def stop_when_ready(*_args):
            if len(self.receiver.events) >= count:
                loop.quit()

        self.loader.thumbnail_ready.connect(stop_when_ready)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()
        self.loader.thumbnail_ready.disconnect(stop_when_ready)
        return len(self.receiver.events) >= count


if __name__ == "__main__":
    unittest.main()
