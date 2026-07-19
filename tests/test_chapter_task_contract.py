import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from jm_downloader import downloader
from jm_downloader.models import TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.task_store import (
    TASK_STORE_SCHEMA_VERSION,
    StoredTask,
    TaskStore,
    UnsupportedTaskStoreVersion,
)
from jm_downloader.tasks import InvalidChapterSelection, TaskManager


class CapturingWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.selected_chapter_ids = kwargs.get("selected_chapter_ids")
        self.callbacks = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, _timeout=None):
        return True


class ChapterTaskContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        CapturingWorker.instances.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_task_manager_preserves_explicit_selection_for_worker_and_snapshot(self):
        manager = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=CapturingWorker,
        )
        try:
            created = manager.add(
                "123",
                selected_chapter_ids=("301", "303"),
            )

            self.assertEqual(
                created.selected_chapter_ids,
                ("301", "303"),
            )
            self.assertEqual(len(CapturingWorker.instances), 1)
            worker = CapturingWorker.instances[0]
            self.assertEqual(worker.selected_chapter_ids, ("301", "303"))
            self.assertTrue(worker.started)

            manager.pause(created.id)
            worker.callbacks["on_stopped"]("123")
            manager.resume(created.id)

            self.assertEqual(len(CapturingWorker.instances), 2)
            self.assertEqual(
                CapturingWorker.instances[1].selected_chapter_ids,
                ("301", "303"),
            )
        finally:
            manager.shutdown(timeout=2)

    def test_selection_is_canonical_deduplicated_and_nonempty(self):
        manager = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=CapturingWorker,
        )
        try:
            created = manager.add(
                "123",
                selected_chapter_ids=("00301", "301", "303"),
            )
            self.assertEqual(created.selected_chapter_ids, ("301", "303"))
            self.assertEqual(
                CapturingWorker.instances[0].selected_chapter_ids,
                ("301", "303"),
            )

            for invalid in ((), "301", ("invalid",)):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(InvalidChapterSelection):
                        manager.add(
                            "456",
                            selected_chapter_ids=invalid,
                        )
        finally:
            manager.shutdown(timeout=2)

    def test_explicit_selection_survives_process_restart_and_resume(self):
        manager = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=CapturingWorker,
        )
        created = manager.add(
            "123",
            selected_chapter_ids=("301", "303"),
        )
        worker = CapturingWorker.instances[0]
        manager.pause(created.id)
        worker.callbacks["on_stopped"]("123")
        self.assertTrue(manager.shutdown(timeout=2))

        CapturingWorker.instances.clear()
        restored = TaskManager(
            paths=self.paths,
            max_concurrent=1,
            worker_factory=CapturingWorker,
        )
        try:
            snapshot = restored.list_tasks()[0]
            self.assertEqual(
                snapshot.selected_chapter_ids,
                ("301", "303"),
            )
            restored.resume(snapshot.id)
            self.assertEqual(
                CapturingWorker.instances[0].selected_chapter_ids,
                ("301", "303"),
            )
        finally:
            restored.shutdown(timeout=2)

    def test_schema_v2_round_trip_preserves_selected_chapter_ids(self):
        self.assertEqual(TASK_STORE_SCHEMA_VERSION, 2)
        store = TaskStore(self.paths)
        task = StoredTask(
            id="abc12345",
            album_id="123",
            title="测试漫画",
            status=TaskStatus.PAUSED,
            progress=27,
            chapter="第一章",
            page="2/8",
            error=None,
            pictures_directory="Pictures",
            pdf_directory="PDFs",
            selected_chapter_ids=("301", "303"),
        )
        try:
            store.save((task,))
            self.assertTrue(store.flush(timeout=2))
            payload = json.loads(
                self.paths.tasks_file.read_text(encoding="utf-8")
            )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(
                payload["tasks"][0]["selected_chapter_ids"],
                ["301", "303"],
            )
            self.assertEqual(TaskStore(self.paths).load(), [task])
        finally:
            store.close(timeout=2)

    def test_schema_v1_task_migrates_to_legacy_whole_album_semantics(self):
        payload = {
            "schema_version": 1,
            "tasks": [
                {
                    "id": "abc12345",
                    "album_id": "123",
                    "title": "旧任务",
                    "status": "paused",
                    "progress": 40,
                    "chapter": "",
                    "page": "",
                    "error": None,
                    "paths": {
                        "pictures": "Pictures",
                        "pdfs": "PDFs",
                    },
                }
            ],
        }
        self.paths.tasks_file.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        store = TaskStore(self.paths)
        manager = TaskManager(
            paths=self.paths,
            worker_factory=CapturingWorker,
            task_store=store,
        )
        try:
            restored = manager.list_tasks()
            self.assertEqual(len(restored), 1)
            self.assertIsNone(restored[0].selected_chapter_ids)
            self.assertTrue(store.flush(timeout=2))
            migrated = json.loads(
                self.paths.tasks_file.read_text(encoding="utf-8")
            )
            self.assertEqual(migrated["schema_version"], 2)
            self.assertIsNone(
                migrated["tasks"][0]["selected_chapter_ids"]
            )
        finally:
            manager.shutdown(timeout=2)

    def test_schema_newer_than_v2_is_refused_without_rewrite(self):
        raw = b'{"schema_version": 3, "tasks": []}'
        self.paths.tasks_file.write_bytes(raw)

        with self.assertRaises(UnsupportedTaskStoreVersion):
            TaskStore(self.paths).load()

        self.assertEqual(self.paths.tasks_file.read_bytes(), raw)


class ChapterDownloadWorkerContractTests(unittest.TestCase):
    def test_worker_filters_album_by_photo_id_instead_of_title_or_position(self):
        selected_seen = []
        errors = []
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            option = Mock()
            option.dir_rule = Mock()
            option.build_jm_client.return_value = MagicMock()
            worker = downloader.DownloadWorker(
                "123",
                selected_chapter_ids=("302", "304"),
                on_error=lambda _album_id, message: errors.append(message),
                paths=paths,
            )
            worker._make_option = Mock(return_value=option)

            def fake_download(
                _album_id,
                received_option,
                downloader: type,
                check_exception,
            ):
                self.assertFalse(check_exception)
                active = downloader(received_option)
                album = MagicMock()
                album.is_album.return_value = True
                album.id = "123"
                album.page_count = 0
                album.title = "测试漫画"
                album.name = "测试漫画"
                album.cover = None
                photos = []
                for photo_id, index in (
                    ("301", 1),
                    ("302", 2),
                    ("303", 3),
                    ("304", 4),
                ):
                    photo = MagicMock()
                    photo.id = photo_id
                    photo.photo_id = photo_id
                    photo.index = index
                    photo.__len__.return_value = 1
                    photos.append(photo)
                album.__iter__.return_value = iter(photos)
                album.__len__.return_value = len(photos)

                filtered = tuple(active.do_filter(album))
                selected_seen.extend(photo.photo_id for photo in filtered)
                raise RuntimeError("stop after filter contract")

            with patch.object(
                downloader.jmcomic,
                "download_album",
                side_effect=fake_download,
            ):
                worker.run()

        self.assertEqual(selected_seen, ["302", "304"])
        self.assertEqual(errors, ["下载失败，请检查网络或稍后继续"])

    def test_missing_persisted_chapter_fails_before_any_image_download(self):
        image_downloaded = []
        errors = []
        with tempfile.TemporaryDirectory() as temp_dir:
            option = Mock()
            option.dir_rule = Mock()
            client = MagicMock()
            option.build_jm_client.return_value = client
            worker = downloader.DownloadWorker(
                "123",
                selected_chapter_ids=("999",),
                on_error=lambda _album_id, message: errors.append(message),
                paths=AppPaths(Path(temp_dir)),
            )
            worker._make_option = Mock(return_value=option)

            def fake_download(
                _album_id,
                received_option,
                downloader: type,
                check_exception,
            ):
                self.assertFalse(check_exception)
                active = downloader(received_option)
                active.client.download_by_image_detail.side_effect = (
                    lambda *_args, **_kwargs: image_downloaded.append(True)
                )
                album = MagicMock()
                album.is_album.return_value = True
                album.id = "123"
                album.__iter__.return_value = iter(())
                album.__len__.return_value = 0
                tuple(active.do_filter(album))

            with patch.object(
                downloader.jmcomic,
                "download_album",
                side_effect=fake_download,
            ):
                worker.run()

        self.assertEqual(image_downloaded, [])
        self.assertEqual(
            errors,
            ["所选章节已发生变化，请移除任务后重新选择"],
        )


if __name__ == "__main__":
    unittest.main()
