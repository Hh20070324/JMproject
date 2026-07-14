import os
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.qt.controllers import SearchController
from jm_downloader.search import SearchResponseError, SearchUnavailable


def search_page(
    request: SearchRequest,
    album_id: str = "1449491",
    *,
    total: int = 1,
    page_count: int = 1,
) -> SearchPageSnapshot:
    item = SearchResultSnapshot(album_id, f"Title {album_id}", ("Author",), ())
    return SearchPageSnapshot(request, total, page_count, (item,))


def empty_page(request: SearchRequest) -> SearchPageSnapshot:
    return SearchPageSnapshot(request, 0, 0, ())


class ControlledSearchService:
    def __init__(self, behavior=None):
        self.behavior = behavior or search_page
        self.calls = []
        self.call_threads = []
        self._lock = threading.Lock()

    def search(self, request: SearchRequest) -> SearchPageSnapshot:
        with self._lock:
            self.calls.append(request)
            self.call_threads.append(threading.current_thread())
        return self.behavior(request)


class SearchControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["search-controller-tests"])

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

    def make_controller(
        self,
        service,
        *,
        worker_count: int = 2,
    ) -> SearchController:
        controller = SearchController(
            service,
            worker_count=worker_count,
            result_interval_ms=5,
        )
        self.controllers.append(controller)
        return controller

    def test_search_and_first_client_creation_run_off_qt_main_thread(self):
        client_created_threads = []
        receiving_threads = []

        def behavior(request):
            if not client_created_threads:
                client_created_threads.append(threading.current_thread())
            return search_page(request)

        service = ControlledSearchService(behavior)
        controller = self.make_controller(service)
        results = []
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: (
                results.append((generation, snapshot, page_change)),
                receiving_threads.append(QThread.currentThread()),
            )
        )

        generation = controller.search(SearchMode.GENERAL, "keyword")
        self.assertEqual(generation, 1)
        self.assertTrue(self.wait_until(lambda: bool(results)))

        self.assertIsNot(service.call_threads[0], threading.main_thread())
        self.assertIsNot(client_created_threads[0], threading.main_thread())
        self.assertEqual(receiving_threads, [self.app.thread()])
        self.assertEqual(results[0][0], 1)
        self.assertFalse(results[0][2])

    def test_submission_result_and_busy_signals_have_stable_order(self):
        service = ControlledSearchService()
        controller = self.make_controller(service)
        events = []
        controller.search_submitted.connect(
            lambda generation, request: events.append(
                ("submitted", generation, request)
            )
        )
        controller.busy_changed.connect(
            lambda busy: events.append(("busy", busy))
        )
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: events.append(
                ("result", generation, snapshot, page_change)
            )
        )

        controller.search(SearchMode.AUTHOR, "Author")
        self.assertTrue(
            self.wait_until(lambda: any(event[0] == "result" for event in events))
        )

        self.assertEqual(events[0][0], "submitted")
        self.assertEqual(events[1], ("busy", True))
        self.assertEqual(events[2], ("busy", False))
        self.assertEqual(events[3][0], "result")
        self.assertFalse(controller.is_busy)

    def test_empty_page_uses_empty_signal_and_updates_snapshot(self):
        service = ControlledSearchService(empty_page)
        controller = self.make_controller(service)
        empty_events = []
        result_events = []
        controller.empty_results.connect(
            lambda generation, snapshot, page_change: empty_events.append(
                (generation, snapshot, page_change)
            )
        )
        controller.results_ready.connect(result_events.append)

        controller.search(SearchMode.TAG, "no-results")
        self.assertTrue(self.wait_until(lambda: bool(empty_events)))

        self.assertEqual(result_events, [])
        self.assertEqual(empty_events[0][0], 1)
        self.assertEqual(controller.current_snapshot, empty_events[0][1])
        self.assertEqual(controller.current_request.query, "no-results")

    def test_pending_slot_keeps_only_latest_unstarted_request(self):
        both_started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        started = []
        lock = threading.Lock()

        def behavior(request):
            if request.query.startswith("running"):
                with lock:
                    started.append(request.query)
                    if len(started) == 2:
                        both_started.set()
                release.wait(timeout=3)
            album_id = request.query[-1] if request.query[-1].isdigit() else "9"
            return search_page(request, album_id)

        service = ControlledSearchService(behavior)
        controller = self.make_controller(service)
        results = []
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: results.append(
                (generation, snapshot.request.query)
            )
        )

        controller.search(SearchMode.GENERAL, "running-1")
        self.assertTrue(self.wait_until(lambda: len(started) == 1))
        controller.search(SearchMode.GENERAL, "running-2")
        self.assertTrue(both_started.wait(timeout=1))
        controller.search(SearchMode.GENERAL, "pending-3")
        controller.search(SearchMode.GENERAL, "pending-4")
        latest_generation = controller.search(SearchMode.GENERAL, "pending-5")

        self.assertEqual(controller.pending_request_count, 1)
        self.assertEqual(controller.worker_count, 2)
        self.assertTrue(controller.workers_are_daemon)
        release.set()
        self.assertTrue(self.wait_until(lambda: bool(results)))

        self.assertEqual(results, [(latest_generation, "pending-5")])
        self.assertEqual(
            [request.query for request in service.calls],
            ["running-1", "running-2", "pending-5"],
        )

    def test_slow_old_success_cannot_replace_fast_new_failure(self):
        old_started = threading.Event()
        release_old = threading.Event()
        self.release_events.append(release_old)

        def behavior(request):
            if request.query == "old":
                old_started.set()
                release_old.wait(timeout=3)
                return search_page(request, "1")
            raise SearchUnavailable("secret query URL")

        controller = self.make_controller(ControlledSearchService(behavior))
        results = []
        failures = []
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: results.append(generation)
        )
        controller.search_failed.connect(
            lambda *args: failures.append(args)
        )

        controller.search(SearchMode.GENERAL, "old")
        self.assertTrue(old_started.wait(timeout=1))
        latest_generation = controller.search(SearchMode.GENERAL, "new")
        self.assertTrue(self.wait_until(lambda: bool(failures)))
        release_old.set()
        self.process_for(50)

        self.assertEqual(results, [])
        self.assertEqual(failures, [(
            latest_generation,
            "unavailable",
            "网络暂不可用，请稍后重试",
            False,
        )])
        self.assertNotIn("secret", failures[0][2])

    def test_slow_old_failure_cannot_replace_fast_new_success(self):
        old_started = threading.Event()
        release_old = threading.Event()
        self.release_events.append(release_old)

        def behavior(request):
            if request.query == "old":
                old_started.set()
                release_old.wait(timeout=3)
                raise SearchResponseError()
            return search_page(request, "2")

        controller = self.make_controller(ControlledSearchService(behavior))
        results = []
        failures = []
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: results.append(
                (generation, snapshot.request.query)
            )
        )
        controller.search_failed.connect(lambda *args: failures.append(args))

        controller.search(SearchMode.GENERAL, "old")
        self.assertTrue(old_started.wait(timeout=1))
        latest_generation = controller.search(SearchMode.GENERAL, "new")
        self.assertTrue(self.wait_until(lambda: bool(results)))
        release_old.set()
        self.process_for(50)

        self.assertEqual(results, [(latest_generation, "new")])
        self.assertEqual(failures, [])
        self.assertEqual(controller.current_request.query, "new")

    def test_page_failure_preserves_current_page_and_retry_semantics(self):
        page_two_attempts = 0

        def behavior(request):
            nonlocal page_two_attempts
            if request.page == 1:
                return search_page(request, "1", total=3, page_count=3)
            page_two_attempts += 1
            if page_two_attempts == 1:
                raise SearchUnavailable()
            return search_page(request, "2", total=3, page_count=3)

        controller = self.make_controller(ControlledSearchService(behavior))
        results = []
        failures = []
        controller.results_ready.connect(
            lambda generation, snapshot, page_change: results.append(
                (generation, snapshot, page_change)
            )
        )
        controller.search_failed.connect(lambda *args: failures.append(args))

        controller.search(SearchMode.GENERAL, "paged")
        self.assertTrue(self.wait_until(lambda: len(results) == 1))
        first_snapshot = controller.current_snapshot

        failed_generation = controller.change_page(2)
        self.assertTrue(self.wait_until(lambda: bool(failures)))

        self.assertEqual(failures[0][0], failed_generation)
        self.assertTrue(failures[0][3])
        self.assertIs(controller.current_snapshot, first_snapshot)
        self.assertEqual(controller.current_request.page, 1)

        retry_generation = controller.retry()
        self.assertTrue(self.wait_until(lambda: len(results) == 2))
        self.assertEqual(results[-1][0], retry_generation)
        self.assertTrue(results[-1][2])
        self.assertEqual(controller.current_request.page, 2)

    def test_invalid_input_does_not_create_generation_or_replace_result(self):
        service = ControlledSearchService()
        controller = self.make_controller(service)
        validations = []
        controller.validation_failed.connect(
            lambda code, message: validations.append((code, message))
        )

        controller.search(SearchMode.GENERAL, "valid")
        self.assertTrue(
            self.wait_until(lambda: controller.current_snapshot is not None)
        )
        snapshot = controller.current_snapshot
        generation = controller.generation

        result = controller.search(SearchMode.GENERAL, "   ")

        self.assertIsNone(result)
        self.assertEqual(controller.generation, generation)
        self.assertIs(controller.current_snapshot, snapshot)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(validations, [("validation", "搜索内容不能为空")])

    def test_wrong_service_result_becomes_stable_response_failure(self):
        request_from_service = SearchRequest(SearchMode.GENERAL, "other", 1)
        service = ControlledSearchService(
            lambda _request: search_page(request_from_service)
        )
        controller = self.make_controller(service)
        failures = []
        controller.search_failed.connect(lambda *args: failures.append(args))

        generation = controller.search(SearchMode.GENERAL, "expected")
        self.assertTrue(self.wait_until(lambda: bool(failures)))

        self.assertEqual(failures, [(
            generation,
            "invalid_response",
            "上游响应暂时无法解析",
            False,
        )])

    def test_dispose_is_nonblocking_and_suppresses_late_results(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)

        def behavior(request):
            started.set()
            release.wait(timeout=3)
            return search_page(request)

        service = ControlledSearchService(behavior)
        controller = self.make_controller(service, worker_count=1)
        results = []
        failures = []
        controller.results_ready.connect(lambda *args: results.append(args))
        controller.search_failed.connect(lambda *args: failures.append(args))

        controller.search(SearchMode.GENERAL, "running")
        self.assertTrue(started.wait(timeout=1))
        controller.search(SearchMode.GENERAL, "pending")
        self.assertEqual(controller.pending_request_count, 1)

        before = time.perf_counter()
        controller.dispose()
        elapsed = time.perf_counter() - before

        self.assertLess(elapsed, 0.1)
        self.assertEqual(controller.pending_request_count, 0)
        self.assertFalse(controller.is_busy)
        generation = controller.generation
        self.assertIsNone(controller.search(SearchMode.GENERAL, "ignored"))
        self.assertEqual(controller.generation, generation)
        release.set()
        self.process_for(80)
        self.assertEqual(results, [])
        self.assertEqual(failures, [])

    def test_constructor_rejects_unbounded_worker_configuration(self):
        service = ControlledSearchService()
        for worker_count in (0, 5, True):
            with self.subTest(worker_count=worker_count):
                with self.assertRaises(ValueError):
                    SearchController(service, worker_count=worker_count)

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
