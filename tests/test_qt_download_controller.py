import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.library import LibraryService
from jm_downloader.models import TaskStatus
from jm_downloader.qt.controllers import DownloadController
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskManager


class ControlledWorker:
    instances = []

    def __init__(self, album_id, **callbacks):
        self.album_id = album_id
        self.callbacks = callbacks
        self.started = False
        self.stopped = False
        self.stop_thread = None
        self.wait_thread = None
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.stop_thread = threading.current_thread()

    def wait(self, _timeout):
        self.wait_thread = threading.current_thread()
        return True


class DownloadControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["controller-tests"])

    def setUp(self):
        ControlledWorker.instances = []
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=ControlledWorker,
        )
        self.library = LibraryService(self.paths)
        self.controller = DownloadController(
            self.manager,
            self.library,
            event_interval_ms=10,
            reconcile_interval_ms=100,
        )

    def tearDown(self):
        self.controller.shutdown(timeout=1)
        self.controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_add_normalizes_id_and_publishes_fetching_snapshot(self):
        events = []
        self.controller.tasks_reset.connect(events.append)

        snapshot = self.controller.add_task(" JM123456 ")

        self.assertEqual(snapshot.album_id, "123456")
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertEqual(events[-1][0], snapshot)
        self.assertTrue(ControlledWorker.instances[0].started)

    def test_invalid_command_emits_failure(self):
        errors = []
        self.controller.command_failed.connect(
            lambda command, message: errors.append((command, message))
        )

        self.assertIsNone(self.controller.add_task("12/34"))

        self.assertEqual(errors, [("add", "车号只能包含数字")])

    def test_progress_burst_is_merged_into_one_latest_snapshot(self):
        self.controller.add_task("1")
        worker = ControlledWorker.instances[0]
        worker.callbacks["on_info"]("1", "标题", None)
        events = []
        self.controller.tasks_reset.connect(events.append)

        for percent in range(80):
            worker.callbacks["on_progress"]("1", percent, "章节", str(percent))
        self.assertTrue(self._wait_until(lambda: bool(events)))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0].progress, 79)
        self.assertEqual(events[0][0].page, "79")

    def test_retry_and_remove_follow_core_state_rules(self):
        snapshot = self.controller.add_task("1")
        worker = ControlledWorker.instances[0]
        worker.callbacks["on_error"]("1", "失败")
        self.assertTrue(
            self._wait_until(
                lambda: self.controller.list_tasks()[0].status
                == TaskStatus.FAILED
            )
        )

        self.controller.retry_task(snapshot.id)
        self.assertEqual(self.controller.list_tasks()[0].status, TaskStatus.FETCHING)
        self.assertEqual(len(ControlledWorker.instances), 2)

        errors = []
        self.controller.command_failed.connect(
            lambda command, message: errors.append((command, message))
        )
        self.controller.remove_task(snapshot.id)
        self.assertEqual(errors[0][0], "remove")

        ControlledWorker.instances[1].callbacks["on_error"]("1", "再次失败")
        self.controller.remove_task(snapshot.id)
        self.assertEqual(self.controller.list_tasks(), [])

    def test_pause_resume_and_cancel_follow_worker_stopped_boundary(self):
        snapshot = self.controller.add_task("1")
        worker = ControlledWorker.instances[0]

        self.controller.pause_task(snapshot.id)
        self.assertEqual(
            self.controller.list_tasks()[0].status,
            TaskStatus.PAUSING,
        )
        self.assertTrue(worker.stopped)

        worker.callbacks["on_stopped"]("1")
        self.assertEqual(
            self.controller.list_tasks()[0].status,
            TaskStatus.PAUSED,
        )

        self.controller.resume_task(snapshot.id)
        replacement = ControlledWorker.instances[1]
        self.assertEqual(
            self.controller.list_tasks()[0].status,
            TaskStatus.FETCHING,
        )

        self.controller.cancel_task(snapshot.id)
        self.assertEqual(
            self.controller.list_tasks()[0].status,
            TaskStatus.CANCELLING,
        )
        replacement.callbacks["on_stopped"]("1")
        self.assertEqual(self.controller.list_tasks(), [])

    def test_open_os_error_is_reported_as_command_failure(self):
        errors = []
        self.controller.command_failed.connect(
            lambda command, message: errors.append((command, message))
        )
        with patch.object(
            self.library,
            "open_location",
            side_effect=OSError("没有默认程序"),
        ):
            self.controller.open_item("1", "pdf")

        self.assertEqual(errors, [("open", "没有默认程序")])

    def test_begin_shutdown_waits_off_main_thread_and_signals_main_thread(self):
        self.controller.add_task("1")
        results = []
        receiving_threads = []

        def on_finished(result):
            results.append(result)
            receiving_threads.append(QThread.currentThread())

        self.controller.shutdown_finished.connect(on_finished)
        self.controller.begin_shutdown(timeout=1)
        self.assertTrue(self._wait_until(lambda: bool(results)))

        worker = ControlledWorker.instances[0]
        self.assertTrue(worker.stopped)
        self.assertIsNot(worker.stop_thread, threading.main_thread())
        self.assertIsNotNone(worker.wait_thread)
        self.assertIsNot(worker.wait_thread, threading.main_thread())
        self.assertEqual(receiving_threads, [self.app.thread()])
        self.assertEqual(results, [True])

    def test_shutdown_exception_still_unblocks_window_with_false_result(self):
        self.controller.add_task("1")
        worker = ControlledWorker.instances[0]

        def fail_wait(_timeout):
            raise RuntimeError("wait failed")

        worker.wait = fail_wait
        results = []
        self.controller.shutdown_finished.connect(results.append)
        self.controller.begin_shutdown(timeout=1)

        self.assertTrue(self._wait_until(lambda: bool(results)))
        self.assertEqual(results, [False])

    def test_sync_shutdown_retries_worker_after_async_timeout(self):
        self.controller.add_task("1")
        worker = ControlledWorker.instances[0]
        wait_results = iter((False, True))
        wait_threads = []

        def wait_twice(_timeout):
            wait_threads.append(threading.current_thread())
            return next(wait_results)

        worker.wait = wait_twice
        results = []
        self.controller.shutdown_finished.connect(results.append)
        self.controller.begin_shutdown(timeout=0.1)
        self.assertTrue(self._wait_until(lambda: bool(results)))

        self.assertEqual(results, [False])
        self.assertTrue(self.controller.shutdown(timeout=1))
        self.assertEqual(len(wait_threads), 2)
        self.assertIsNot(wait_threads[0], threading.main_thread())
        self.assertIs(wait_threads[1], threading.main_thread())

    def _wait_until(self, predicate, timeout_ms: int = 2000) -> bool:
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


if __name__ == "__main__":
    unittest.main()
