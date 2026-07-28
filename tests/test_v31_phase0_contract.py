import tempfile
import unittest
from pathlib import Path

from jm_downloader.library import LibraryNotFound, LibraryService
from jm_downloader.models import TaskStatus
from jm_downloader.qt.controllers.reader_controller import _PageJob
from jm_downloader.settings import (
    AppPaths,
    AppSettings,
    SETTINGS_SCHEMA_VERSION,
    UnsupportedSettingsVersion,
)
from jm_downloader.tasks import TaskManager


class V31Phase0ContractTests(unittest.TestCase):
    def test_reader_cache_identity_includes_target_width(self):
        first = _PageJob(7, "301", 2, 20, 2, 800, 0, 1)
        second = _PageJob(7, "301", 2, 20, 2, 1200, 0, 2)

        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.cache_key, second.cache_key)
        self.assertEqual(first.disk_page_key, second.disk_page_key)

    def test_task_manager_reserves_every_non_completed_scope(self):
        self.assertEqual(
            set(TaskManager.RESERVED_STATUSES),
            {
                TaskStatus.PENDING.value,
                TaskStatus.FETCHING.value,
                TaskStatus.DOWNLOADING.value,
                TaskStatus.PAUSING.value,
                TaskStatus.PAUSED.value,
                TaskStatus.CANCELLING.value,
                TaskStatus.FAILED.value,
            },
        )
        self.assertNotIn(
            TaskStatus.COMPLETED.value,
            TaskManager.RESERVED_STATUSES,
        )

    def test_completed_chapter_detection_is_offline_and_rejects_bad_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths(Path(temporary))
            service = LibraryService(paths)

            self.assertEqual(
                service.completed_chapter_ids("123"),
                frozenset(),
            )
            with self.assertRaises(LibraryNotFound):
                service.completed_chapter_ids("../123")

    def test_settings_schema_accepts_missing_optional_groups_but_not_future(self):
        self.assertEqual(SETTINGS_SCHEMA_VERSION, 1)
        self.assertEqual(
            AppSettings.from_dict({"schema_version": 1}),
            AppSettings(),
        )
        with self.assertRaises(UnsupportedSettingsVersion):
            AppSettings.from_dict({"schema_version": 2})


if __name__ == "__main__":
    unittest.main()
