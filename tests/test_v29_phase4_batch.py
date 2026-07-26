import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from jm_downloader.library import ChapterManifestStore, LibraryService
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    TaskStatus,
)
from jm_downloader.settings import AppPaths
from jm_downloader.task_store import TaskStore
from jm_downloader.tasks import (
    TaskBatchValidationError,
    TaskManager,
)


class ControlledWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.callbacks = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, _timeout):
        return True


class Phase4BatchTaskTests(unittest.TestCase):
    def setUp(self):
        ControlledWorker.instances = []
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.manager = TaskManager(
            paths=self.paths,
            max_concurrent=3,
            worker_factory=ControlledWorker,
        )

    def tearDown(self):
        self.manager.shutdown(timeout=1)
        self.temporary.cleanup()

    def test_selection_splits_in_order_and_same_album_runs_serially(self):
        selected = tuple(str(value) for value in range(1, 24))

        snapshots = self.manager.add_batch(
            "123",
            selected_chapter_ids=selected,
        )

        self.assertEqual(
            [snapshot.selected_chapter_ids for snapshot in snapshots],
            [selected[:10], selected[10:20], selected[20:]],
        )
        self.assertEqual(len(ControlledWorker.instances), 1)
        first = ControlledWorker.instances[0]
        first.callbacks["on_error"]("123", "第一批失败")

        self.assertEqual(len(ControlledWorker.instances), 2)
        current = self.manager.list_tasks()
        self.assertEqual(current[0].status, TaskStatus.FAILED)
        self.assertEqual(current[1].status, TaskStatus.FETCHING)

    def test_different_albums_still_use_global_parallelism(self):
        self.manager.add_batch(
            "123",
            selected_chapter_ids=("1", "2"),
        )
        self.manager.add_batch(
            "456",
            selected_chapter_ids=("3", "4"),
        )

        self.assertEqual(
            [worker.album_id for worker in ControlledWorker.instances],
            ["123", "456"],
        )

    def test_overlap_rejects_the_whole_new_batch_with_structured_issues(self):
        self.manager.add_batch(
            "123",
            selected_chapter_ids=("1", "2"),
        )
        before = self.manager.list_tasks()

        with self.assertRaises(TaskBatchValidationError) as caught:
            self.manager.add_batch(
                "123",
                selected_chapter_ids=("2", "3", "4"),
            )

        self.assertEqual(self.manager.list_tasks(), before)
        self.assertEqual(caught.exception.issues[0].chapter_ids, ("2",))

    def test_legacy_whole_album_task_overlaps_every_explicit_chapter(self):
        self.manager.add("123")

        with self.assertRaises(TaskBatchValidationError) as caught:
            self.manager.add_batch(
                "123",
                selected_chapter_ids=("8", "9"),
            )

        self.assertEqual(
            caught.exception.issues[0].chapter_ids,
            ("8", "9"),
        )

    def test_force_redownload_ids_are_split_and_forwarded_to_worker(self):
        selected = tuple(str(value) for value in range(1, 13))
        snapshots = self.manager.add_batch(
            "123",
            selected_chapter_ids=selected,
            force_redownload_chapter_ids=("2", "11"),
        )

        self.assertEqual(
            snapshots[0].force_redownload_chapter_ids,
            ("2",),
        )
        self.assertEqual(
            snapshots[1].force_redownload_chapter_ids,
            ("11",),
        )
        self.assertEqual(
            ControlledWorker.instances[0].callbacks[
                "force_redownload_chapter_ids"
            ],
            ("2",),
        )

    def test_album_delete_stops_other_worker_and_requeues_it(self):
        first, target = self.manager.add_batch(
            "123",
            selected_chapter_ids=tuple(
                str(value) for value in range(1, 12)
            ),
        )
        first_worker = ControlledWorker.instances[0]

        self.manager.prepare_album_delete(target.id)

        self.assertTrue(first_worker.stopped)
        self.assertFalse(self.manager.is_cancel_ready(target.id))
        first_worker.callbacks["on_stopped"]("123")
        self.assertTrue(self.manager.is_cancel_ready(target.id))
        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.PENDING,
        )

        self.manager.finish_cancel(target.id)

        self.assertEqual(len(self.manager.list_tasks()), 1)
        self.assertEqual(len(ControlledWorker.instances), 2)
        self.assertEqual(
            self.manager.get_task(first.id).status,
            TaskStatus.FETCHING,
        )


class Phase4DownloadedDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.paths.ensure_output_directories()

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_and_verified_images_are_both_required(self):
        title_dir = self.paths.pictures / "123" / "Album"
        complete_dir = title_dir / "第1章"
        corrupt_dir = title_dir / "第2章"
        complete_dir.mkdir(parents=True)
        corrupt_dir.mkdir()
        for index in (1, 2):
            Image.new("RGB", (4, 4), "green").save(
                complete_dir / f"{index:03}.jpg"
            )
        (corrupt_dir / "001.jpg").write_bytes(b"not-an-image")
        manifest = ChapterManifest(
            version=2,
            album_id="123",
            album_title="Album",
            album_dir_name="Album",
            chapters=(
                ChapterManifestEntry(
                    "301",
                    1,
                    "Complete",
                    "第1章",
                    2,
                    "jpg",
                ),
                ChapterManifestEntry(
                    "302",
                    2,
                    "Corrupt",
                    "第2章",
                    1,
                    "jpg",
                ),
            ),
        )
        ChapterManifestStore(self.paths).replace_exact(manifest)

        completed = LibraryService(self.paths).completed_chapter_ids("123")

        self.assertEqual(completed, frozenset({"301"}))

    def test_old_layout_without_manifest_degrades_to_no_detection(self):
        legacy = self.paths.pictures / "123" / "Old title"
        legacy.mkdir(parents=True)
        Image.new("RGB", (4, 4), "green").save(legacy / "001.jpg")

        self.assertEqual(
            LibraryService(self.paths).completed_chapter_ids("123"),
            frozenset(),
        )


class Phase4TaskStoreMigrationTests(unittest.TestCase):
    def test_schema_v3_defaults_force_redownload_to_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths(Path(temporary))
            paths.ensure_output_directories()
            payload = {
                "schema_version": 3,
                "tasks": [
                    {
                        "id": "abc12345",
                        "album_id": "123",
                        "title": None,
                        "status": "paused",
                        "progress": 0,
                        "chapter": "",
                        "page": "",
                        "error": None,
                        "selected_chapter_ids": ["301"],
                        "download": {
                            "engine": "sync",
                            "api_route": "auto",
                            "package_format": "pdf",
                            "image_format": "jpg",
                            "image_concurrency": 16,
                            "multi_chapter_download_behavior": "parallel",
                        },
                        "paths": {
                            "pictures": "Pictures",
                            "pdfs": "PDFs",
                        },
                    }
                ],
            }
            paths.tasks_file.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            store = TaskStore(paths)
            restored = store.load()

            self.assertEqual(
                restored[0].force_redownload_chapter_ids,
                (),
            )
            self.assertTrue(store.needs_migration)


if __name__ == "__main__":
    unittest.main()
