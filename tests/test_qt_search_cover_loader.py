import os
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QSize, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from jm_downloader.qt.widgets.search_cover_loader import (
    MAX_COVER_BYTES,
    MAX_SOURCE_DIMENSION,
    SearchCoverLoader,
)


def _image_bytes(width: int = 120, height: int = 180) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#247a52"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("could not encode fixture")
    buffer.close()
    return bytes(data)


class _ImmediateService:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def fetch_cover(self, album_id: str) -> bytes:
        self.calls.append(album_id)
        response = self.responses.get(album_id, _image_bytes())
        if isinstance(response, Exception):
            raise response
        return response


class _BlockingService:
    def __init__(self, content: bytes | None = None):
        self.content = content or _image_bytes()
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.max_active = 0

    def fetch_cover(self, album_id: str) -> bytes:
        with self.lock:
            self.calls.append(album_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
        self.release.wait(3)
        with self.lock:
            self.active -= 1
        return self.content


class SearchCoverLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["search-cover-tests"])

    def setUp(self):
        self.loaders = []
        self.ready = []
        self.failed = []

    def tearDown(self):
        for loader in self.loaders:
            loader.dispose()
            loader.deleteLater()
        self.app.processEvents()

    def _loader(self, service, **kwargs) -> SearchCoverLoader:
        loader = SearchCoverLoader(service, **kwargs)
        loader.cover_ready.connect(
            lambda generation, album_id, image: self.ready.append(
                (generation, album_id, image)
            )
        )
        loader.cover_failed.connect(
            lambda generation, album_id: self.failed.append(
                (generation, album_id)
            )
        )
        self.loaders.append(loader)
        return loader

    def test_workers_are_fixed_daemons_and_decode_to_target_size(self):
        service = _ImmediateService({"1": _image_bytes(200, 100)})
        loader = self._loader(service, worker_count=2)

        self.assertEqual(loader.worker_count, 2)
        self.assertTrue(loader.workers_are_daemon)
        self.assertTrue(loader.request(1, "JM1", QSize(64, 64)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 1))

        generation, album_id, image = self.ready[0]
        self.assertEqual((generation, album_id), (1, "1"))
        self.assertEqual(image.size(), QSize(64, 32))
        self.assertEqual(service.calls, ["1"])

    def test_inflight_requests_share_fetch_across_generations(self):
        service = _BlockingService()
        loader = self._loader(service, worker_count=1)

        self.assertTrue(loader.request(3, "42", QSize(90, 90)))
        self.assertTrue(service.started.wait(1))
        self.assertTrue(loader.request(4, "JM42", QSize(90, 90)))
        self.assertTrue(loader.request(4, "42", QSize(90, 90)))
        service.release.set()

        self.assertTrue(self._wait_until(lambda: len(self.ready) == 2))
        self.assertEqual([event[0] for event in self.ready], [3, 4])
        self.assertEqual(service.calls, ["42"])

    def test_old_generation_remains_tagged_for_ui_filtering(self):
        service = _BlockingService()
        loader = self._loader(service, worker_count=1)
        displayed = []
        current_generation = 8
        loader.cover_ready.connect(
            lambda generation, album_id, _image: (
                displayed.append(album_id)
                if generation == current_generation
                else None
            )
        )

        self.assertTrue(loader.request(7, "55", QSize(80, 80)))
        self.assertTrue(service.started.wait(1))
        self.assertTrue(loader.request(8, "55", QSize(80, 80)))
        service.release.set()

        self.assertTrue(self._wait_until(lambda: len(self.ready) == 2))
        self.assertEqual([event[0] for event in self.ready], [7, 8])
        self.assertEqual(displayed, ["55"])

    def test_lru_caches_only_matching_scaled_size_and_evicts_oldest(self):
        service = _ImmediateService()
        loader = self._loader(service, worker_count=1, cache_capacity=1)

        self.assertTrue(loader.request(1, "10", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 1))
        self.assertEqual(loader.cache_size, 1)
        self.assertTrue(loader.request(2, "10", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 2))
        self.assertEqual(service.calls, ["10"])

        self.assertTrue(loader.request(3, "11", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 3))
        self.assertEqual(loader.cache_size, 1)
        self.assertTrue(loader.request(4, "10", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 4))
        self.assertEqual(service.calls, ["10", "11", "10"])

        self.assertTrue(loader.request(5, "10", QSize(30, 30)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 5))
        self.assertEqual(service.calls[-1], "10")
        self.assertEqual(self.ready[-1][2].size(), QSize(20, 30))

    def test_cached_deliveries_and_generation_listeners_are_bounded(self):
        service = _BlockingService()
        loader = self._loader(service, worker_count=1, queue_capacity=1)

        self.assertTrue(loader.request(1, "20", QSize(60, 60)))
        self.assertTrue(service.started.wait(1))
        self.assertTrue(loader.request(2, "20", QSize(60, 60)))
        self.assertFalse(loader.request(3, "20", QSize(60, 60)))
        service.release.set()
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 2))

        self.assertTrue(loader.request(3, "20", QSize(60, 60)))
        self.assertFalse(loader.request(4, "20", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 3))
        self.assertTrue(loader.request(4, "20", QSize(60, 60)))
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 4))
        self.assertEqual(service.calls, ["20"])

    def test_worker_concurrency_and_waiting_queue_are_bounded(self):
        service = _BlockingService()
        loader = self._loader(
            service,
            worker_count=2,
            queue_capacity=2,
        )

        self.assertTrue(loader.request(1, "1", QSize(50, 50)))
        self.assertTrue(loader.request(1, "2", QSize(50, 50)))
        self.assertTrue(
            self._wait_until(lambda: service.active == 2, process_events=False)
        )
        self.assertTrue(loader.request(1, "3", QSize(50, 50)))
        self.assertTrue(loader.request(1, "4", QSize(50, 50)))
        self.assertFalse(loader.request(1, "5", QSize(50, 50)))
        self.assertEqual(loader.inflight_count, 4)
        self.assertLessEqual(service.max_active, 2)

        service.release.set()
        self.assertTrue(self._wait_until(lambda: len(self.ready) == 4))
        self.assertLessEqual(service.max_active, 2)
        self.assertNotIn("5", service.calls)

    def test_bad_payloads_and_network_failure_affect_only_their_card(self):
        too_wide = _image_bytes(MAX_SOURCE_DIMENSION + 1, 1)
        service = _ImmediateService(
            {
                "1": b"not an image",
                "2": b"",
                "3": b"x" * (MAX_COVER_BYTES + 1),
                "4": too_wide,
                "5": OSError("offline"),
                "6": _image_bytes(),
            }
        )
        loader = self._loader(service, worker_count=2, queue_capacity=8)

        for album_id in service.responses:
            self.assertTrue(loader.request(1, album_id, QSize(80, 80)))

        self.assertTrue(
            self._wait_until(lambda: len(self.failed) + len(self.ready) == 6)
        )
        self.assertEqual({album_id for _, album_id in self.failed}, set("12345"))
        self.assertEqual([(g, album) for g, album, _ in self.ready], [(1, "6")])
        self.assertEqual(loader.cache_size, 1)

    def test_invalid_request_is_rejected_without_fetch(self):
        service = _ImmediateService()
        loader = self._loader(service)

        self.assertFalse(loader.request(-1, "1", QSize(20, 20)))
        self.assertFalse(loader.request(1, "bad-id", QSize(20, 20)))
        self.assertFalse(loader.request(1, "1", QSize()))
        self.assertFalse(loader.request(1, "1", QSize(5000, 20)))
        self.assertEqual(service.calls, [])

    def test_dispose_returns_without_waiting_and_drops_late_result(self):
        service = _BlockingService()
        loader = self._loader(service, worker_count=1)

        self.assertTrue(loader.request(1, "99", QSize(80, 80)))
        self.assertTrue(service.started.wait(1))
        started = time.perf_counter()
        loader.dispose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.2)
        self.assertFalse(loader.request(2, "100", QSize(80, 80)))
        service.release.set()
        self._wait_until(lambda: False, timeout_ms=150)
        self.assertEqual(self.ready, [])
        self.assertEqual(self.failed, [])

    def _wait_until(
        self,
        predicate,
        timeout_ms: int = 3000,
        *,
        process_events: bool = True,
    ) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        if not process_events:
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.005)
            return bool(predicate())

        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        return bool(predicate())


if __name__ == "__main__":
    unittest.main()
