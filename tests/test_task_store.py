import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jm_downloader.models import TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.task_store import (
    StoredTask,
    TaskStore,
    TaskStoreError,
    UnsupportedTaskStoreVersion,
)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(self.root)
        self.store = TaskStore(self.paths)

    def tearDown(self):
        self.store.close(timeout=2)
        self.temp_dir.cleanup()

    @staticmethod
    def _task(**changes):
        values = {
            "id": "abc12345",
            "album_id": "123456",
            "title": "测试漫画",
            "status": TaskStatus.DOWNLOADING,
            "progress": 42,
            "chapter": "第一章",
            "page": "4/10",
            "error": None,
            "pictures_directory": "Pictures",
            "pdf_directory": "PDFs",
        }
        values.update(changes)
        return StoredTask(**values)

    def test_missing_store_loads_empty_without_creating_a_file(self):
        self.assertEqual(self.store.load(), [])
        self.assertFalse(self.paths.tasks_file.exists())

    def test_load_cleans_only_stale_task_store_temporary_files(self):
        stale = self.root / ".tasks.json.interrupted.tmp"
        unrelated = self.root / ".settings.json.interrupted.tmp"
        stale.write_bytes(b"partial")
        unrelated.write_bytes(b"keep")

        self.assertEqual(self.store.load(), [])

        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())

    def test_round_trip_uses_utf8_and_preserves_task_order(self):
        first = self._task()
        second = self._task(
            id="def67890",
            album_id="654321",
            title="第二本",
            status=TaskStatus.FAILED,
            error="网络暂不可用",
        )

        self.store.save((first, second))
        self.assertTrue(self.store.flush(timeout=2))

        raw = self.paths.tasks_file.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("测试漫画", raw.decode("utf-8"))
        self.assertEqual(TaskStore(self.paths).load(), [first, second])

    def test_relative_paths_follow_a_moved_portable_root(self):
        task = self._task(
            pictures_directory="data/Pictures",
            pdf_directory="data/PDFs",
        )
        moved_root = self.root / "moved"

        resolved = task.to_paths(moved_root)

        self.assertEqual(resolved.pictures, (moved_root / "data/Pictures").resolve())
        self.assertEqual(resolved.pdfs, (moved_root / "data/PDFs").resolve())

    def test_corrupt_store_is_backed_up_and_replaced_with_empty_schema(self):
        raw = b'{"schema_version": 1, broken'
        self.paths.tasks_file.write_bytes(raw)

        self.assertEqual(self.store.load(), [])

        backup = self.store.last_recovery_backup
        self.assertIsNotNone(backup)
        self.assertEqual(backup.read_bytes(), raw)
        restored = json.loads(self.paths.tasks_file.read_text(encoding="utf-8"))
        self.assertEqual(restored, {"schema_version": 2, "tasks": []})

    def test_future_schema_is_refused_without_rewrite_or_backup(self):
        raw = b'{"schema_version": 3, "tasks": []}'
        self.paths.tasks_file.write_bytes(raw)

        with self.assertRaises(UnsupportedTaskStoreVersion):
            self.store.load()

        self.assertEqual(self.paths.tasks_file.read_bytes(), raw)
        self.assertEqual(list(self.root.glob("tasks.json.corrupt-*")), [])

    def test_invalid_escape_path_is_treated_as_corruption(self):
        task = self._task().to_dict()
        task["paths"]["pictures"] = "../outside"
        data = {
            "schema_version": 2,
            "tasks": [task],
        }
        self.paths.tasks_file.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )

        self.assertEqual(self.store.load(), [])
        self.assertIsNotNone(self.store.last_recovery_backup)

    def test_failed_atomic_replace_preserves_previous_file(self):
        original = self._task()
        self.store.save((original,))
        self.assertTrue(self.store.flush(timeout=2))
        before = self.paths.tasks_file.read_bytes()

        with patch.object(
            self.store,
            "_write_atomic",
            side_effect=TaskStoreError("commit failed"),
        ):
            self.store.save((self._task(progress=99),))
            self.assertFalse(self.store.flush(timeout=2))

        self.assertEqual(self.paths.tasks_file.read_bytes(), before)
        self.assertIn("commit failed", str(self.store.last_error))

    def test_duplicate_ids_are_rejected_before_writing(self):
        with self.assertRaisesRegex(TaskStoreError, "不能重复"):
            self.store.save(
                (
                    self._task(),
                    self._task(album_id="654321"),
                )
            )
        self.assertFalse(self.paths.tasks_file.exists())


if __name__ == "__main__":
    unittest.main()
