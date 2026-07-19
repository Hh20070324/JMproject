import os
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.models import ChapterCatalogSnapshot, ChapterSnapshot
from jm_downloader.qt.controllers import ChapterCatalogController
from jm_downloader.search import SearchUnavailable


def chapter_catalog(album_id: str = "123") -> ChapterCatalogSnapshot:
    return ChapterCatalogSnapshot(
        album_id,
        f"Title {album_id}",
        (
            ChapterSnapshot(f"{album_id}01", 1, "First"),
            ChapterSnapshot(f"{album_id}02", 2, "Second"),
        ),
    )


class ControlledChapterService:
    def __init__(self, behavior=None):
        self.behavior = behavior or chapter_catalog
        self.calls = []
        self.call_threads = []
        self._lock = threading.Lock()

    def fetch_chapters(self, album_id: str) -> ChapterCatalogSnapshot:
        with self._lock:
            self.calls.append(album_id)
            self.call_threads.append(threading.current_thread())
        return self.behavior(album_id)


class ChapterCatalogControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["chapter-catalog-controller-tests"]
        )

    def setUp(self):
        self.controllers = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for controller in self.controllers:
            controller.dispose()
            controller.deleteLater()
        self.app.processEvents()

    def make_controller(self, service) -> ChapterCatalogController:
        controller = ChapterCatalogController(service)
        self.controllers.append(controller)
        return controller

    def test_request_fetches_on_background_thread_and_delivers_on_qt_thread(self):
        service = ControlledChapterService()
        controller = self.make_controller(service)
        deliveries = []
        delivery_threads = []
        controller.catalog_ready.connect(
            lambda request_id, catalog: (
                deliveries.append((request_id, catalog)),
                delivery_threads.append(QThread.currentThread()),
            )
        )

        request_id = controller.request(" JM00123 ")

        self.assertIsInstance(request_id, int)
        self.assertTrue(self.wait_until(lambda: bool(deliveries)))
        self.assertEqual(service.calls, ["123"])
        self.assertIsNot(service.call_threads[0], threading.main_thread())
        self.assertEqual(deliveries, [(request_id, chapter_catalog("123"))])
        self.assertEqual(delivery_threads, [self.app.thread()])
        self.assertFalse(controller.is_busy("123"))

    def test_same_album_inflight_requests_are_deduplicated(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)

        def slow_fetch(album_id):
            started.set()
            release.wait(timeout=3)
            return chapter_catalog(album_id)

        service = ControlledChapterService(slow_fetch)
        controller = self.make_controller(service)
        busy_events = []
        deliveries = []
        controller.busy_changed.connect(
            lambda album_id, busy: busy_events.append((album_id, busy))
        )
        controller.catalog_ready.connect(
            lambda *args: deliveries.append(args)
        )

        first_id = controller.request("JM00123")
        self.assertTrue(started.wait(timeout=1))
        duplicate_id = controller.request("123")

        self.assertEqual(duplicate_id, first_id)
        self.assertTrue(controller.is_busy(" JM00123 "))
        self.assertEqual(service.calls, ["123"])
        self.assertEqual(busy_events, [("123", True)])

        release.set()
        self.assertTrue(self.wait_until(lambda: bool(deliveries)))
        self.assertEqual(deliveries, [(first_id, chapter_catalog("123"))])
        self.assertEqual(busy_events, [("123", True), ("123", False)])
        self.assertFalse(controller.is_busy("123"))

    def test_completed_catalog_is_cached_for_the_session(self):
        service = ControlledChapterService()
        controller = self.make_controller(service)
        deliveries = []
        controller.catalog_ready.connect(
            lambda *args: deliveries.append(args)
        )

        first_id = controller.request("123")
        self.assertTrue(self.wait_until(lambda: len(deliveries) == 1))
        second_id = controller.request("123")

        self.assertEqual(len(deliveries), 1)
        self.assertTrue(self.wait_until(lambda: len(deliveries) == 2))
        self.assertEqual(service.calls, ["123"])
        self.assertEqual(deliveries[0], (first_id, chapter_catalog("123")))
        self.assertEqual(deliveries[1], (second_id, chapter_catalog("123")))
        self.assertFalse(controller.is_busy("123"))

    def test_prime_makes_the_first_cache_hit_asynchronous(self):
        service = ControlledChapterService()
        controller = self.make_controller(service)
        catalog = chapter_catalog("123")
        controller.prime(catalog)
        deliveries = []
        busy_events = []
        controller.catalog_ready.connect(
            lambda *args: deliveries.append(args)
        )
        controller.busy_changed.connect(
            lambda *args: busy_events.append(args)
        )

        request_id = controller.request("123")

        self.assertEqual(deliveries, [])
        self.assertTrue(self.wait_until(lambda: bool(deliveries)))
        self.assertEqual(deliveries, [(request_id, catalog)])
        self.assertEqual(service.calls, [])
        self.assertEqual(busy_events, [])
        self.assertFalse(controller.is_busy("123"))

    def test_failure_has_stable_payload_and_clears_busy_state(self):
        secret = "secret backend URL and response"

        def fail(_album_id):
            raise SearchUnavailable(secret)

        controller = self.make_controller(ControlledChapterService(fail))
        failures = []
        busy_events = []
        controller.catalog_failed.connect(
            lambda *args: failures.append(args)
        )
        controller.busy_changed.connect(
            lambda *args: busy_events.append(args)
        )

        request_id = controller.request("123")

        self.assertTrue(self.wait_until(lambda: bool(failures)))
        self.assertEqual(
            failures,
            [(request_id, "unavailable", "网络暂不可用，请稍后重试")],
        )
        self.assertNotIn(secret, repr(failures))
        self.assertEqual(busy_events, [("123", True), ("123", False)])
        self.assertFalse(controller.is_busy("123"))

    def test_dispose_is_nonblocking_and_ignores_late_result(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)

        def slow_fetch(album_id):
            started.set()
            release.wait(timeout=3)
            return chapter_catalog(album_id)

        controller = self.make_controller(
            ControlledChapterService(slow_fetch)
        )
        deliveries = []
        failures = []
        controller.catalog_ready.connect(
            lambda *args: deliveries.append(args)
        )
        controller.catalog_failed.connect(
            lambda *args: failures.append(args)
        )

        controller.request("123")
        self.assertTrue(started.wait(timeout=1))

        before = time.perf_counter()
        controller.dispose()
        elapsed = time.perf_counter() - before

        self.assertLess(elapsed, 0.1)
        self.assertFalse(controller.is_busy("123"))
        self.assertIsNone(controller.request("456"))
        release.set()
        self.process_for(80)
        self.assertEqual(deliveries, [])
        self.assertEqual(failures, [])

    def wait_until(self, predicate, timeout_ms: int = 2000) -> bool:
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
        return predicate()

    def process_for(self, duration_ms: int) -> None:
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(duration_ms)
        loop.exec()


if __name__ == "__main__":
    unittest.main()
