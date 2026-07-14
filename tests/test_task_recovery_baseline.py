import tempfile
import unittest
from pathlib import Path

from jm_downloader.library import LibraryService
from jm_downloader.models import TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.task_store import StoredTask, TaskStore
from jm_downloader.tasks import TaskManager

from tests.task_recovery_support import ControlledWorker


class TaskRecoveryBaselineTests(unittest.TestCase):
    def setUp(self):
        ControlledWorker.reset()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=ControlledWorker,
        )

    def tearDown(self):
        for worker in ControlledWorker.instances:
            worker.finish()
        self.manager.shutdown(timeout=1)
        self.temp_dir.cleanup()

    def test_local_library_is_discovered_independently_from_task_rows(self):
        image = self.paths.pictures / "123456" / "第一章" / "1.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"image")

        self.assertEqual(self.manager.list_tasks(), [])
        item = LibraryService(self.paths).get_item("123456")
        self.assertEqual(item.album_id, "123456")
        self.assertEqual(item.image_count, 1)
        self.assertEqual(item.preview_path, image.resolve())

    def test_empty_manager_shutdown_does_not_create_task_history(self):
        self.assertTrue(self.manager.shutdown(timeout=1))
        self.assertFalse(self.paths.tasks_file.exists())

    def test_task_worker_uses_the_managed_output_paths(self):
        self.manager.add("123456")

        self.assertEqual(len(ControlledWorker.instances), 1)
        worker = ControlledWorker.instances[0]
        self.assertTrue(worker.started)
        self.assertEqual(worker.paths, self.paths)

    def test_interrupted_task_restores_paused_without_starting_a_worker(self):
        self.manager.add("123456")
        worker = ControlledWorker.instances[0]
        worker.emit_info()
        worker.emit_progress(40, page="4/10")
        worker.finish()
        self.assertTrue(self.manager.shutdown(timeout=2))

        ControlledWorker.reset()
        restored = TaskManager(
            paths=self.paths,
            worker_factory=ControlledWorker,
        )
        self.manager = restored

        tasks = restored.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, TaskStatus.PAUSED)
        self.assertEqual(tasks[0].progress, 40)
        self.assertEqual(tasks[0].page, "4/10")
        self.assertEqual(ControlledWorker.instances, [])
        self.assertFalse(restored.has_active_tasks())

    def test_unclean_active_record_is_normalized_to_paused_on_load(self):
        self.assertTrue(self.manager.shutdown(timeout=2))
        store = TaskStore(self.paths)
        store.save(
            (
                StoredTask(
                    id="abc12345",
                    album_id="123456",
                    title="测试漫画",
                    status=TaskStatus.DOWNLOADING,
                    progress=55,
                    chapter="第二章",
                    page="5/10",
                    error=None,
                    pictures_directory="Pictures",
                    pdf_directory="PDFs",
                ),
            )
        )
        self.assertTrue(store.close(timeout=2))

        ControlledWorker.reset()
        restored = TaskManager(
            paths=self.paths,
            worker_factory=ControlledWorker,
        )
        self.manager = restored

        snapshot = restored.list_tasks()[0]
        self.assertEqual(snapshot.status, TaskStatus.PAUSED)
        self.assertEqual(ControlledWorker.instances, [])
        self.assertTrue(restored.task_store.flush(timeout=2))
        persisted = TaskStore(self.paths).load()[0]
        self.assertEqual(persisted.status, TaskStatus.PAUSED)

    def test_completed_task_is_not_restored(self):
        task = self.manager.add("123456")
        pdf_path = self.paths.pdfs / "123456.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"pdf")
        worker = ControlledWorker.instances[0]
        worker.emit_complete(pdf_path)
        self.assertEqual(
            self.manager.get_task(task.id).status,
            TaskStatus.COMPLETED,
        )
        self.assertTrue(self.manager.shutdown(timeout=2))

        ControlledWorker.reset()
        restored = TaskManager(
            paths=self.paths,
            worker_factory=ControlledWorker,
        )
        self.manager = restored
        self.assertEqual(restored.list_tasks(), [])
        self.assertEqual(ControlledWorker.instances, [])

    def test_task_record_keeps_the_original_output_directories(self):
        old_pictures = self.paths.root / "old-pictures"
        old_pdfs = self.paths.root / "old-pdfs"
        original_paths = AppPaths(
            self.paths.root,
            pictures_override=old_pictures,
            pdfs_override=old_pdfs,
        )
        self.assertTrue(self.manager.shutdown(timeout=2))
        self.manager = TaskManager(
            paths=original_paths,
            worker_factory=ControlledWorker,
        )
        self.manager.add("123456")
        ControlledWorker.instances[-1].finish()
        self.assertTrue(self.manager.shutdown(timeout=2))

        stored = TaskStore(self.paths).load()[0]
        bound_paths = stored.to_paths(self.paths.root)
        self.assertEqual(bound_paths.pictures, old_pictures.resolve())
        self.assertEqual(bound_paths.pdfs, old_pdfs.resolve())


if __name__ == "__main__":
    unittest.main()
