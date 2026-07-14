import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from jm_downloader.models import TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.task_store import StoredTask, TaskStore
from jm_downloader.tasks import InvalidTaskState, TaskManager

from tests.task_recovery_support import ControlledWorker


class TaskStateMachineTests(unittest.TestCase):
    def setUp(self):
        ControlledWorker.reset()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=ControlledWorker,
        )

    def tearDown(self):
        for worker in ControlledWorker.instances:
            worker.finish()
        self.manager.shutdown(timeout=1)
        self.temp_dir.cleanup()

    def test_active_pause_waits_for_worker_stopped_before_freeing_slot(self):
        first = self.manager.add("1")
        second = self.manager.add("2")
        worker = ControlledWorker.instances[0]

        self.manager.pause(first.id)

        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.PAUSING,
        )
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.PENDING,
        )
        self.assertTrue(worker.stopped)
        self.assertEqual(len(ControlledWorker.instances), 1)

        worker.emit_stopped()

        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.PAUSED,
        )
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.FETCHING,
        )
        self.assertEqual(len(ControlledWorker.instances), 2)

    def test_pause_during_worker_creation_does_not_leave_pausing_state(self):
        factory_entered = threading.Event()
        release_factory = threading.Event()

        def blocking_factory(album_id, **kwargs):
            factory_entered.set()
            release_factory.wait(timeout=2)
            return ControlledWorker(album_id, **kwargs)

        self.manager.shutdown(timeout=1)
        self.manager = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=blocking_factory,
        )
        add_thread = threading.Thread(target=lambda: self.manager.add("9"))
        add_thread.start()
        self.assertTrue(factory_entered.wait(timeout=1))
        task = self.manager.list_tasks()[0]

        self.manager.pause(task.id)
        release_factory.set()
        add_thread.join(timeout=2)

        self.assertFalse(add_thread.is_alive())
        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.PAUSED,
        )
        self.assertFalse(ControlledWorker.instances[-1].started)
        self.assertTrue(ControlledWorker.instances[-1].stopped)

    def test_pending_pause_is_immediate_and_resume_rejoins_queue(self):
        first = self.manager.add("1")
        second = self.manager.add("2")

        self.manager.pause(second.id)
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.PAUSED,
        )

        self.manager.resume(second.id)
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.PENDING,
        )

        ControlledWorker.instances[0].emit_error()
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.FETCHING,
        )
        self.assertEqual(first.album_id, "1")

    def test_resume_keeps_progress_and_task_bound_directories(self):
        old_pictures = self.paths.root / "old-pictures"
        old_pdfs = self.paths.root / "old-pdfs"
        bound_paths = AppPaths(
            self.paths.root,
            pictures_override=old_pictures,
            pdfs_override=old_pdfs,
        )
        self.manager.shutdown(timeout=1)
        self.manager = TaskManager(
            paths=bound_paths,
            max_concurrent=1,
            worker_factory=ControlledWorker,
        )
        task = self.manager.add("3")
        worker = ControlledWorker.instances[-1]
        worker.emit_progress(62, page="6/10")
        self.manager.pause(task.id)
        worker.emit_stopped()

        self.manager.resume(task.id)

        snapshot = self.manager.get_task(task.id)
        replacement = ControlledWorker.instances[-1]
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertEqual(snapshot.progress, 62)
        self.assertEqual(snapshot.page, "6/10")
        self.assertEqual(replacement.paths.pictures, old_pictures.resolve())
        self.assertEqual(replacement.paths.pdfs, old_pdfs.resolve())

    def test_active_cancel_waits_for_worker_and_then_removes_task(self):
        first = self.manager.add("1")
        second = self.manager.add("2")
        worker = ControlledWorker.instances[0]

        self.manager.cancel(first.id)

        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.CANCELLING,
        )
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.PENDING,
        )
        self.assertTrue(worker.stopped)

        worker.emit_stopped()

        tasks = self.manager.list_tasks()
        self.assertEqual([task.id for task in tasks], [second.id])
        self.assertEqual(tasks[0].status, TaskStatus.FETCHING)

    def test_deferred_cancel_keeps_record_until_cleanup_succeeds(self):
        task = self.manager.add("1")
        worker = ControlledWorker.instances[0]

        self.manager.prepare_cancel(task.id)
        worker.emit_stopped()

        self.assertTrue(self.manager.is_cancel_ready(task.id))
        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.CANCELLING,
        )
        self.manager.finish_cancel(task.id)
        self.assertEqual(self.manager.list_tasks(), [])

    def test_failed_deferred_cleanup_preserves_task_as_failed(self):
        task = self.manager.add("1")
        worker = ControlledWorker.instances[0]
        self.manager.prepare_cancel(task.id)
        worker.emit_stopped()

        self.manager.fail_cancel(task.id, "删除失败")

        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.status, TaskStatus.FAILED)
        self.assertEqual(snapshot.error, "删除失败")
        self.assertFalse(self.manager.is_cancel_ready(task.id))

    def test_completion_callback_cannot_override_pause_intent(self):
        task = self.manager.add("1")
        worker = ControlledWorker.instances[0]
        pdf = self.paths.pdfs / "1.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"pdf")

        self.manager.pause(task.id)
        worker.emit_complete(pdf)

        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.PAUSING,
        )
        worker.emit_stopped()
        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.PAUSED,
        )

    def test_cancel_paused_task_removes_record_but_keeps_files(self):
        image = self.paths.pictures / "1" / "chapter" / "1.jpg"
        pdf = self.paths.pdfs / "1.pdf"
        image.parent.mkdir(parents=True)
        pdf.parent.mkdir(parents=True)
        image.write_bytes(b"image")
        pdf.write_bytes(b"pdf")
        task = self.manager.add("1")
        worker = ControlledWorker.instances[0]
        self.manager.pause(task.id)
        worker.emit_stopped()

        self.manager.cancel(task.id)

        self.assertEqual(self.manager.list_tasks(), [])
        self.assertTrue(image.is_file())
        self.assertTrue(pdf.is_file())

    def test_transitional_states_reject_duplicate_commands(self):
        task = self.manager.add("1")
        self.manager.pause(task.id)

        with self.assertRaises(InvalidTaskState):
            self.manager.pause(task.id)
        with self.assertRaises(InvalidTaskState):
            self.manager.resume(task.id)
        with self.assertRaises(InvalidTaskState):
            self.manager.cancel(task.id)

    def test_unexpected_worker_stop_becomes_failed(self):
        task = self.manager.add("1")

        ControlledWorker.instances[0].emit_stopped()

        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.status, TaskStatus.FAILED)
        self.assertIn("意外停止", snapshot.error)

    def test_stop_all_pauses_running_and_pending_tasks(self):
        first = self.manager.add("1")
        second = self.manager.add("2")
        worker = ControlledWorker.instances[0]

        self.manager.stop_all()

        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.PAUSING,
        )
        self.assertEqual(
            self.manager.get_task(second.id).status,
            TaskStatus.PAUSED,
        )
        worker.emit_stopped()
        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.PAUSED,
        )

    def test_restore_preview_scans_valid_image_without_starting_worker(self):
        task = self.manager.add("1")
        worker = ControlledWorker.instances[0]
        self.manager.pause(task.id)
        worker.emit_stopped()
        image = self.paths.pictures / "1" / "chapter" / "1.jpg"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), "white").save(image, "JPEG")

        restored = self.manager.restore_preview(task.id)

        self.assertEqual(restored, image.resolve())
        snapshot = self.manager.get_task(task.id)
        self.assertEqual(snapshot.preview_path, image.resolve())
        self.assertEqual(snapshot.preview_revision, 1)
        self.assertEqual(len(ControlledWorker.instances), 1)


class TransitionalStateRecoveryTests(unittest.TestCase):
    def test_pausing_and_cancelling_records_restore_as_paused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            records = tuple(
                StoredTask(
                    id=f"task{index}",
                    album_id=str(index),
                    title=None,
                    status=status,
                    progress=20 * index,
                    chapter="",
                    page="",
                    error=None,
                    pictures_directory="Pictures",
                    pdf_directory="PDFs",
                )
                for index, status in enumerate(
                    (TaskStatus.PAUSING, TaskStatus.CANCELLING),
                    start=1,
                )
            )
            store = TaskStore(paths)
            store.save(records)
            self.assertTrue(store.close(timeout=1))

            ControlledWorker.reset()
            manager = TaskManager(
                paths=paths,
                worker_factory=ControlledWorker,
            )
            try:
                self.assertEqual(
                    [task.status for task in manager.list_tasks()],
                    [TaskStatus.PAUSED, TaskStatus.PAUSED],
                )
                self.assertEqual(ControlledWorker.instances, [])
            finally:
                manager.shutdown(timeout=1)


if __name__ == "__main__":
    unittest.main()
