import os
from pathlib import Path
import tempfile
import threading
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.library import LibraryError
from jm_downloader.models import (
    ChapterImageStatus,
    ChapterPackageStatus,
    ChapterRepairBatch,
    ChapterRepairPlan,
    LibraryChapterSnapshot,
    LibraryItem,
    LibraryLayout,
    TaskConfig,
    TaskStatus,
)
from jm_downloader.qt.controllers import LibraryController
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskConflict, TaskManager


def library_item(album_id: str) -> LibraryItem:
    return LibraryItem(
        album_id=album_id,
        title=f"测试漫画 {album_id}",
        layout=LibraryLayout.MANAGED,
        chapter_count=1,
        image_count=2,
        image_size=100,
        preview_path=Path(f"Pictures/{album_id}/1/1.jpg"),
        pdf_directory=None,
        pdf_size=0,
    )


class PassiveWorker:
    instances = []

    def __init__(self, album_id, **callbacks):
        self.album_id = album_id
        self.callbacks = callbacks
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def wait(self, _timeout):
        return True


class RejectingThreadPool(QThreadPool):
    def start(self, *args, **kwargs):
        raise RuntimeError("thread pool rejected work")


class ControlledLibrary:
    def __init__(self):
        self.items = []
        self.scan_plan = []
        self.scan_threads = []
        self.calls = []
        self.operation_started = threading.Event()
        self.operation_release = threading.Event()
        self.operation_release.set()
        self.operation_error = None
        self._lock = threading.Lock()

    def list_items(self):
        self.scan_threads.append(threading.current_thread())
        with self._lock:
            plan = self.scan_plan.pop(0) if self.scan_plan else None
        if plan is not None:
            started, release, result = plan
            if started is not None:
                started.set()
            if release is not None:
                release.wait(timeout=3)
            return list(result)
        return list(self.items)

    def open_location(self, album_id, kind):
        self.calls.append(("open", album_id, kind, threading.current_thread()))

    def delete_images(self, album_id):
        return self._mutate("delete_images", album_id)

    def delete_pdf(self, album_id):
        return self._mutate("delete_pdf", album_id)

    def delete_all(self, album_id):
        return self._mutate("delete_all", album_id)

    def delete_chapter(self, album_id, photo_id, kind, *, expected=None):
        self.calls.append(
            (
                f"delete_chapter_{kind}",
                album_id,
                photo_id,
                expected,
                threading.current_thread(),
            )
        )
        self.operation_started.set()
        self.operation_release.wait(timeout=3)
        if self.operation_error is not None:
            raise self.operation_error
        return {"photo_id": photo_id, "kind": kind}

    def check_chapters(self, album_id):
        self.calls.append(("check_chapters", album_id, threading.current_thread()))
        self.operation_started.set()
        self.operation_release.wait(timeout=3)
        return (
            LibraryChapterSnapshot(
                album_id=album_id,
                photo_id="301",
                index=1,
                title="第一章",
                image_directory=Path("Pictures/1/title"),
                package_path=None,
                page_count=1,
                valid_image_count=0,
                image_status=ChapterImageStatus.MISSING,
                package_format="pdf",
                package_status=ChapterPackageStatus.MISSING,
                downloaded_at_utc=None,
                can_rebuild=False,
                can_redownload=True,
                can_delete_images=False,
                can_delete_package=False,
                can_delete_all=True,
            ),
        )

    def plan_chapter_repairs(
        self,
        album_id,
        photo_ids,
        *,
        confirmed_formats=None,
    ):
        self.calls.append(
            (
                "plan_chapter_repairs",
                album_id,
                tuple(photo_ids),
                dict(confirmed_formats or {}),
                threading.current_thread(),
            )
        )
        return ChapterRepairPlan(
            album_id=album_id,
            rebuild_photo_ids=(),
            download_batches=(
                ChapterRepairBatch("pdf", tuple(photo_ids)),
            ),
            unchanged_photo_ids=(),
            failures=(),
        )

    def _mutate(self, command, album_id):
        self.calls.append((command, album_id, threading.current_thread()))
        self.operation_started.set()
        self.operation_release.wait(timeout=3)
        if self.operation_error is not None:
            raise self.operation_error
        return None


class LibraryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["library-controller-tests"])

    def setUp(self):
        PassiveWorker.instances = []
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=PassiveWorker,
        )
        self.library = ControlledLibrary()
        self.thread_pool = QThreadPool()
        self.controller = LibraryController(
            self.manager,
            self.library,
            thread_pool=self.thread_pool,
            event_interval_ms=10,
            reconcile_interval_ms=50,
        )

    def tearDown(self):
        self.controller.shutdown(timeout=3)
        self.manager.shutdown(timeout=1)
        self.controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_refresh_runs_off_main_thread_and_delivers_on_qt_thread(self):
        self.library.items = [library_item("1")]
        events = []
        receiving_threads = []
        self.controller.items_reset.connect(events.append)
        self.controller.items_reset.connect(
            lambda _items: receiving_threads.append(QThread.currentThread())
        )

        self.controller.refresh()
        self.assertTrue(self._wait_until(lambda: bool(events)))

        self.assertEqual([item.album_id for item in events[0]], ["1"])
        self.assertIsNot(self.library.scan_threads[0], threading.main_thread())
        self.assertEqual(receiving_threads, [self.app.thread()])

    def test_refresh_burst_discards_old_result_and_runs_latest_once(self):
        first_started = threading.Event()
        release_first = threading.Event()
        self.library.scan_plan = [
            (first_started, release_first, [library_item("old")]),
            (None, None, [library_item("new")]),
        ]
        events = []
        self.controller.items_reset.connect(events.append)

        self.controller.refresh()
        self.assertTrue(first_started.wait(timeout=1))
        self.controller.refresh()
        self.controller.refresh()
        release_first.set()
        self.assertTrue(
            self._wait_until(
                lambda: events
                and [item.album_id for item in events[-1]] == ["new"]
            )
        )

        self.assertEqual(len(self.library.scan_threads), 2)
        self.assertEqual(
            [[item.album_id for item in event] for event in events],
            [["new"]],
        )

    def test_mutation_reserves_album_until_background_work_finishes(self):
        self.library.operation_release.clear()
        successes = []
        self.controller.operation_succeeded.connect(
            lambda command, album_id: successes.append((command, album_id))
        )

        self.controller.delete_item("1", "all")
        self.assertTrue(self.library.operation_started.wait(timeout=1))

        self.assertEqual(self.controller.busy_album_ids(), frozenset({"1"}))
        with self.assertRaisesRegex(TaskConflict, "本地库操作"):
            self.manager.add("1")
        self.library.operation_release.set()
        self.assertTrue(self._wait_until(lambda: bool(successes)))

        self.assertEqual(successes, [("delete_all", "1")])
        self.assertEqual(self.controller.busy_album_ids(), frozenset())
        self.assertFalse(self.manager.is_library_operation_active("1"))
        self.assertIsNot(self.library.calls[0][2], threading.main_thread())
        self.manager.add("1")

    def test_active_download_rejects_mutation_before_worker_starts(self):
        self.manager.add("1")
        errors = []
        self.controller.command_failed.connect(
            lambda command, album_id, message: errors.append(
                (command, album_id, message)
            )
        )

        self.controller.delete_item("1", "images")

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][:2], ("delete_images", "1"))
        self.assertIn("暂不可修改", errors[0][2])
        self.assertEqual(self.library.calls, [])

    def test_failed_mutation_clears_busy_state_and_reservation(self):
        self.library.operation_error = LibraryError("PDF 文件被占用")
        errors = []
        self.controller.command_failed.connect(
            lambda command, album_id, message: errors.append(
                (command, album_id, message)
            )
        )

        self.controller.delete_item("1", "pdf")
        self.assertTrue(self._wait_until(lambda: bool(errors)))

        self.assertEqual(errors, [("delete_pdf", "1", "PDF 文件被占用")])
        self.assertEqual(self.controller.busy_album_ids(), frozenset())
        self.assertFalse(self.manager.is_library_operation_active("1"))

    def test_chapter_delete_reserves_album_and_returns_background_result(self):
        expected = object()
        outcomes = []
        self.controller.operation_completed.connect(
            lambda command, album_id, result: outcomes.append(
                (command, album_id, result)
            )
        )

        self.controller.delete_chapter(
            "1",
            "301",
            "images",
            expected,
        )
        self.assertTrue(self._wait_until(lambda: bool(outcomes)))

        self.assertEqual(
            outcomes,
            [
                (
                    "delete_chapter_images",
                    "1",
                    {"photo_id": "301", "kind": "images"},
                )
            ],
        )
        call = self.library.calls[0]
        self.assertEqual(call[:4], ("delete_chapter_images", "1", "301", expected))
        self.assertIsNot(call[4], threading.main_thread())
        self.assertFalse(self.manager.is_library_operation_active("1"))

    def test_active_download_rejects_chapter_delete_before_worker_starts(self):
        self.manager.add("1")
        errors = []
        self.controller.command_failed.connect(
            lambda command, album_id, message: errors.append(
                (command, album_id, message)
            )
        )

        self.controller.delete_chapter("1", "301", "all", None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][:2], ("delete_chapter_all", "1"))
        self.assertIn("暂不可修改", errors[0][2])
        self.assertEqual(self.library.calls, [])

    def test_chapter_check_has_request_id_and_runs_off_main_thread(self):
        completed = []
        self.controller.request_completed.connect(
            lambda *values: completed.append(values)
        )

        request_id = self.controller.check_chapters("1")
        self.assertIsInstance(request_id, int)
        self.assertTrue(self._wait_until(lambda: bool(completed)))

        self.assertEqual(completed[0][:3], (request_id, "check_chapters", "1"))
        self.assertEqual(completed[0][3][0].photo_id, "301")
        self.assertIsNot(self.library.calls[0][2], threading.main_thread())
        self.assertFalse(self.manager.is_library_operation_active("1"))

    def test_rejected_chapter_request_reports_its_generation_asynchronously(self):
        self.manager.add("1")
        failures = []
        self.controller.request_failed.connect(
            lambda *values: failures.append(values)
        )

        request_id = self.controller.check_chapters("1")
        self.assertEqual(failures, [])
        self.assertTrue(self._wait_until(lambda: bool(failures)))

        self.assertEqual(
            failures[0][:3],
            (request_id, "check_chapters", "1"),
        )
        self.assertIn("暂不可修改", failures[0][3])
        self.assertEqual(
            [call for call in self.library.calls if call[0] == "check_chapters"],
            [],
        )

    def test_repair_adds_download_tasks_only_after_reservation_is_released(self):
        completed = []
        self.controller.request_completed.connect(
            lambda *values: completed.append(values)
        )

        request_id = self.controller.repair_chapters(
            "1",
            ("301",),
            {},
            TaskConfig(package_format="images"),
        )
        self.assertTrue(self._wait_until(lambda: bool(completed)))

        result = completed[0][3]
        self.assertEqual(completed[0][:3], (request_id, "repair_chapters", "1"))
        self.assertEqual(len(result.created_tasks), 1)
        self.assertEqual(
            result.created_tasks[0].selected_chapter_ids,
            ("301",),
        )
        self.assertEqual(result.created_tasks[0].config.package_format, "pdf")
        self.assertFalse(self.manager.is_library_operation_active("1"))

    def test_batch_delete_is_serial_and_continues_after_one_failure(self):
        calls = []
        active_calls = 0
        max_active_calls = 0
        worker_threads = []

        def delete_all(album_id):
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            calls.append(album_id)
            worker_threads.append(threading.current_thread())
            try:
                if album_id == "2":
                    raise LibraryError("文件被占用")
            finally:
                active_calls -= 1

        self.library.delete_all = delete_all
        outcomes = []
        self.controller.batch_delete_finished.connect(
            lambda kind, succeeded, failures: outcomes.append(
                (kind, tuple(succeeded), tuple(failures))
            )
        )

        self.assertTrue(
            self.controller.batch_delete(("1", "2", "3"), "all")
        )
        self.assertTrue(self._wait_until(lambda: bool(outcomes)))

        self.assertEqual(calls, ["1", "2", "3"])
        self.assertEqual(max_active_calls, 1)
        self.assertTrue(
            all(thread is not threading.main_thread() for thread in worker_threads)
        )
        self.assertEqual(
            outcomes,
            [
                (
                    "all",
                    ("1", "3"),
                    (("2", "文件被占用"),),
                )
            ],
        )
        self.assertEqual(self.controller.busy_album_ids(), frozenset())
        self.assertFalse(
            any(
                self.manager.is_library_operation_active(album_id)
                for album_id in ("1", "2", "3")
            )
        )

    def test_rejected_worker_submission_releases_mutation_reservation(self):
        controller = LibraryController(
            self.manager,
            self.library,
            thread_pool=RejectingThreadPool(),
            event_interval_ms=10,
            reconcile_interval_ms=50,
        )
        errors = []
        controller.command_failed.connect(
            lambda command, album_id, message: errors.append(
                (command, album_id, message)
            )
        )

        controller.delete_item("1", "all")

        self.assertEqual(errors[0][:2], ("delete_all", "1"))
        self.assertIn("thread pool rejected work", errors[0][2])
        self.assertEqual(controller.busy_album_ids(), frozenset())
        self.assertFalse(self.manager.is_library_operation_active("1"))
        controller.shutdown(timeout=1)
        controller.deleteLater()

    def test_completed_download_triggers_library_refresh(self):
        self.library.items = [library_item("1")]
        events = []
        self.controller.items_reset.connect(events.append)
        snapshot = self.manager.add("1")
        pdf_directory = self.paths.pdfs / "1" / "测试漫画"
        pdf_directory.mkdir(parents=True, exist_ok=True)
        (pdf_directory / "第1章.pdf").write_bytes(b"pdf")

        PassiveWorker.instances[0].callbacks["on_complete"](
            "1",
            str(pdf_directory),
        )

        self.assertTrue(self._wait_until(lambda: bool(events)))
        self.assertEqual([item.album_id for item in events[-1]], ["1"])
        self.assertEqual(
            self.manager.get_task(snapshot.id).status,
            TaskStatus.COMPLETED,
        )

    def test_reconcile_refreshes_when_terminal_event_was_dropped(self):
        self.library.items = [library_item("1")]
        events = []
        self.controller.items_reset.connect(events.append)
        snapshot = self.manager.add("1")
        self.controller._drain_task_events()
        self.assertEqual(self.controller.active_album_ids(), frozenset({"1"}))

        for index in range(200):
            self.manager.broadcast({"type": "progress", "percent": index})
        pdf_directory = self.paths.pdfs / "1" / "测试漫画"
        pdf_directory.mkdir(parents=True, exist_ok=True)
        (pdf_directory / "第1章.pdf").write_bytes(b"pdf")
        PassiveWorker.instances[0].callbacks["on_complete"](
            "1",
            str(pdf_directory),
        )

        self.assertEqual(
            self.manager.get_task(snapshot.id).status,
            TaskStatus.COMPLETED,
        )
        self.controller._reconcile_active_albums()
        self.assertTrue(self._wait_until(lambda: bool(events)))
        self.assertEqual([item.album_id for item in events[-1]], ["1"])
        self.assertEqual(self.controller.active_album_ids(), frozenset())

    def test_reconcile_refreshes_when_entire_task_lifecycle_was_dropped(self):
        self.library.items = [library_item("1")]
        events = []
        self.controller.items_reset.connect(events.append)
        for index in range(200):
            self.manager.broadcast({"type": "progress", "percent": index})

        snapshot = self.manager.add("1")
        pdf_directory = self.paths.pdfs / "1" / "测试漫画"
        pdf_directory.mkdir(parents=True, exist_ok=True)
        (pdf_directory / "第1章.pdf").write_bytes(b"pdf")
        PassiveWorker.instances[0].callbacks["on_complete"](
            "1",
            str(pdf_directory),
        )

        self.assertEqual(
            self.manager.get_task(snapshot.id).status,
            TaskStatus.COMPLETED,
        )
        self.assertEqual(self.controller.active_album_ids(), frozenset())
        self.controller._reconcile_active_albums()
        self.assertTrue(self._wait_until(lambda: bool(events)))
        self.assertEqual([item.album_id for item in events[-1]], ["1"])

    def _wait_until(self, predicate, timeout_ms: int = 3000) -> bool:
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
