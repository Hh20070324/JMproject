import tempfile
import threading
import unittest
from pathlib import Path

from jm_downloader.models import TaskSnapshot, TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import (
    InvalidAlbumId,
    InvalidTaskState,
    TaskConflict,
    TaskManager,
)


class WaitingWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.callbacks = kwargs
        self.started = False
        self.stopped = False
        self.wait_timeout = None
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, timeout):
        self.wait_timeout = timeout
        return True


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        WaitingWorker.instances = []
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=WaitingWorker,
        )

    def tearDown(self):
        self.manager.shutdown(timeout=0)
        self.temp_dir.cleanup()

    def test_add_rejects_non_numeric_album_ids(self):
        with self.assertRaisesRegex(InvalidAlbumId, "车号只能包含数字"):
            self.manager.add("12/34")

        self.assertEqual(self.manager.list_tasks(), [])

    def test_add_normalizes_optional_jm_prefix_and_returns_snapshot(self):
        task = self.manager.add(" JM123456 ")
        snapshot = self.manager.get_task(task.id)

        self.assertIsInstance(snapshot, TaskSnapshot)
        self.assertEqual(snapshot.album_id, "123456")
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertIsNone(snapshot.preview_path)

    def test_duplicate_active_task_is_rejected(self):
        self.manager.add("123456")

        with self.assertRaises(TaskConflict):
            self.manager.add("JM123456")

    def test_active_download_blocks_library_operation(self):
        self.manager.add("123456")

        with self.assertRaisesRegex(TaskConflict, "暂不可修改"):
            self.manager.begin_library_operation("123456")

    def test_library_operation_blocks_download_until_released(self):
        album_id = self.manager.begin_library_operation("JM123456")

        with self.assertRaisesRegex(TaskConflict, "本地库操作"):
            self.manager.add(album_id)
        self.assertTrue(self.manager.is_library_operation_active(album_id))

        self.manager.end_library_operation(album_id)
        self.assertEqual(self.manager.add(album_id).album_id, album_id)

    def test_concurrency_limit_and_remove_pending_task(self):
        tasks = [self.manager.add(album_id) for album_id in ("1", "2", "3")]

        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.FETCHING, TaskStatus.FETCHING, TaskStatus.PENDING],
        )

        self.manager.remove(tasks[2].id)
        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.FETCHING, TaskStatus.FETCHING],
        )

    def test_active_task_cannot_be_removed_as_if_stopped_immediately(self):
        task = self.manager.add("1")

        with self.assertRaises(InvalidTaskState):
            self.manager.remove(task.id)
        self.assertFalse(WaitingWorker.instances[0].stopped)

    def test_completed_worker_schedules_next_pending_task(self):
        for album_id in ("1", "2", "3"):
            self.manager.add(album_id)
        pdf_path = self.paths.pdfs / "1.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"pdf")

        WaitingWorker.instances[0].callbacks["on_complete"]("1", str(pdf_path))

        self.assertEqual(len(WaitingWorker.instances), 3)
        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.COMPLETED, TaskStatus.FETCHING, TaskStatus.FETCHING],
        )

    def test_worker_cannot_complete_with_missing_pdf(self):
        task = self.manager.add("1")
        missing_pdf = self.paths.pdfs / "missing.pdf"

        WaitingWorker.instances[0].callbacks["on_complete"](
            "1", str(missing_pdf)
        )

        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.status, TaskStatus.FAILED)
        self.assertEqual(snapshot.error, "PDF 文件不存在")
        self.assertIsNone(snapshot.pdf_path)

    def test_stale_worker_callback_does_not_replace_retry_generation(self):
        task = self.manager.add("1")
        original = WaitingWorker.instances[0]
        original.callbacks["on_error"]("1", "first failure")

        self.manager.retry(task.id)
        replacement = WaitingWorker.instances[1]
        original.callbacks["on_error"]("1", "late stale failure")

        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertIsNone(snapshot.error)
        self.assertTrue(self.manager.shutdown(timeout=1))
        self.assertTrue(replacement.stopped)
        self.assertIsNotNone(replacement.wait_timeout)

    def test_retry_conflicts_with_reserved_library_operation(self):
        task = self.manager.add("1")
        WaitingWorker.instances[0].callbacks["on_error"]("1", "failure")
        album_id = self.manager.begin_library_operation("JM1")

        try:
            with self.assertRaisesRegex(TaskConflict, "本地库操作"):
                self.manager.retry(task.id)
        finally:
            self.manager.end_library_operation(album_id)

        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.FAILED,
        )

    def test_stop_all_notifies_active_workers(self):
        self.manager.add("1")
        self.manager.add("2")

        self.assertTrue(self.manager.has_active_tasks())
        self.manager.stop_all()

        self.assertTrue(
            all(worker.stopped for worker in WaitingWorker.instances)
        )

    def test_shutdown_waits_for_workers_and_stops_new_scheduling(self):
        self.manager.add("1")

        self.assertTrue(self.manager.shutdown(timeout=1))
        worker = WaitingWorker.instances[0]
        self.assertTrue(worker.stopped)
        self.assertIsNotNone(worker.wait_timeout)
        with self.assertRaises(InvalidTaskState):
            self.manager.add("2")

    def test_shutdown_during_worker_creation_prevents_late_start(self):
        factory_entered = threading.Event()
        release_factory = threading.Event()

        def blocking_factory(album_id, **kwargs):
            factory_entered.set()
            release_factory.wait(timeout=2)
            return WaitingWorker(album_id, **kwargs)

        manager = TaskManager(
            paths=self.paths,
            worker_factory=blocking_factory,
        )
        errors = []

        def add_task():
            try:
                manager.add("1")
            except Exception as error:
                errors.append(error)

        add_thread = threading.Thread(target=add_task)
        add_thread.start()
        self.assertTrue(factory_entered.wait(timeout=1))

        self.assertTrue(manager.shutdown(timeout=0))
        release_factory.set()
        add_thread.join(timeout=2)

        self.assertFalse(add_thread.is_alive())
        self.assertEqual(errors, [])
        worker = WaitingWorker.instances[-1]
        self.assertFalse(worker.started)
        self.assertTrue(worker.stopped)

    def test_shutdown_keeps_timed_out_worker_for_second_wait(self):
        class DelayedWorker(WaitingWorker):
            def __init__(self, album_id, **kwargs):
                super().__init__(album_id, **kwargs)
                self.wait_count = 0

            def wait(self, timeout):
                self.wait_timeout = timeout
                self.wait_count += 1
                return self.wait_count >= 2

        manager = TaskManager(paths=self.paths, worker_factory=DelayedWorker)
        manager.add("1")
        worker = WaitingWorker.instances[-1]

        self.assertFalse(manager.shutdown(timeout=0))
        self.assertTrue(manager.shutdown(timeout=0))
        self.assertEqual(worker.wait_count, 2)

    def test_preview_event_and_snapshot_use_local_managed_path(self):
        task = self.manager.add("123456")
        preview_path = self.paths.pictures / "123456" / "chapter" / "1.jpg"
        preview_path.parent.mkdir(parents=True)
        preview_path.write_bytes(b"preview image")
        listener = self.manager.add_listener()
        self.addCleanup(self.manager.remove_listener, listener)

        WaitingWorker.instances[0].callbacks["on_preview"](
            "123456", str(preview_path)
        )

        event = listener.get_nowait()
        self.manager.remove_listener(listener)
        self.assertEqual(event["preview_path"], preview_path.resolve())
        self.assertNotIn("preview", event)
        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.preview_path, preview_path.resolve())
        self.assertEqual(snapshot.preview_revision, 1)

    def test_preview_outside_managed_directory_is_ignored(self):
        task = self.manager.add("123456")
        outside_path = self.paths.root / "outside.jpg"
        outside_path.write_bytes(b"preview image")
        listener = self.manager.add_listener()
        self.addCleanup(self.manager.remove_listener, listener)

        WaitingWorker.instances[0].callbacks["on_preview"](
            "123456", str(outside_path)
        )

        self.assertTrue(listener.empty())
        snapshot = self.manager.get_task(task.id)
        self.assertIsNone(snapshot.preview_path)
        self.assertEqual(snapshot.preview_revision, 0)

    def test_completed_event_and_snapshot_use_local_managed_pdf(self):
        task = self.manager.add("123456")
        pdf_path = self.paths.pdfs / "123456.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"pdf")
        listener = self.manager.add_listener()
        self.addCleanup(self.manager.remove_listener, listener)

        WaitingWorker.instances[0].callbacks["on_complete"](
            "123456", str(pdf_path)
        )

        event = listener.get_nowait()
        self.manager.remove_listener(listener)
        self.assertEqual(event["pdf_path"], pdf_path.resolve())
        self.assertNotIn("pdf", event)
        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.pdf_path, pdf_path.resolve())
        self.assertEqual(snapshot.status, TaskStatus.COMPLETED)

    def test_pdf_outside_managed_directory_fails_task(self):
        task = self.manager.add("123456")
        outside_path = self.paths.root / "outside.pdf"
        outside_path.write_bytes(b"pdf")

        WaitingWorker.instances[0].callbacks["on_complete"](
            "123456", str(outside_path)
        )

        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.status, TaskStatus.FAILED)
        self.assertEqual(snapshot.error, "PDF 输出路径不在受管目录中")
        self.assertIsNone(snapshot.pdf_path)


if __name__ == "__main__":
    unittest.main()
