from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.library import LibraryService
from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterImageStatus,
    ChapterPackageStatus,
    ChapterRepairPlan,
    ChapterSnapshot,
    LibraryChapterSnapshot,
)
from jm_downloader.qt.controllers.library_controller import (
    ChapterRepairExecutionResult,
)
from jm_downloader.qt.pages import LibraryPage
from jm_downloader.qt.widgets.library_chapter_dialogs import (
    LibraryChapterDialog,
)
from jm_downloader.settings import AppPaths


def _chapter(photo_id: str, index: int) -> LibraryChapterSnapshot:
    return LibraryChapterSnapshot(
        album_id="123",
        photo_id=photo_id,
        index=index,
        title=f"可靠性章节 {index}",
        image_directory=Path(f"Pictures/123/title/第{index}章"),
        package_path=None,
        page_count=3,
        valid_image_count=3,
        image_status=ChapterImageStatus.COMPLETE,
        package_format="pdf",
        package_status=ChapterPackageStatus.MISSING,
        downloaded_at_utc=None,
        can_rebuild=True,
        can_redownload=False,
        can_delete_images=True,
        can_delete_package=False,
        can_delete_all=True,
        problem_codes=("package_missing",),
    )


class V291Phase7ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v291-phase7-reliability-tests"]
        )

    def test_legacy_migration_uses_dynamic_managed_path_budget(self):
        with tempfile.TemporaryDirectory() as value:
            paths = AppPaths(Path(value))
            image_path = paths.pictures / "123" / "1.jpg"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (3, 4), "green").save(image_path)
            service = LibraryService(paths)
            long_title = "很长的漫画标题" * 40
            catalog = ChapterCatalogSnapshot(
                album_id="123",
                title=long_title,
                chapters=(ChapterSnapshot("301", 1, "第一章"),),
            )

            plan = service.plan_legacy_migration("123", catalog)

            image_target = (
                paths.pictures
                / "123"
                / plan.album_dir_name
                / "第999999章"
                / "00000.jpg"
            )
            package_target = (
                paths.pdfs
                / "123"
                / plan.album_dir_name
                / f"{plan.album_dir_name}.pdf"
            )
            self.assertLessEqual(len(str(image_target.resolve())), 240)
            self.assertLessEqual(len(str(package_target.resolve())), 240)
            self.assertLess(len(plan.album_dir_name), len(long_title))

    def test_download_repair_summary_does_not_start_conflicting_recheck(self):
        page = LibraryPage(None)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog = LibraryChapterDialog("123", "测试", page)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.set_snapshots((_chapter("301", 1),))
        result = ChapterRepairExecutionResult(
            album_id="123",
            plan=ChapterRepairPlan(
                album_id="123",
                rebuild_photo_ids=(),
                download_batches=(),
                unchanged_photo_ids=(),
                failures=(),
            ),
            rebuild_result=None,
            created_tasks=(SimpleNamespace(id="task-1"),),
        )
        page._chapter_dialogs["123"] = dialog
        page._chapter_requests[7] = (
            "repair_chapters",
            "123",
            dialog,
        )

        with patch.object(page, "_request_chapter_check") as recheck:
            page._on_library_request_completed(
                7,
                "repair_chapters",
                "123",
                result,
            )
        recheck.assert_not_called()
        self.assertIn("下载完成后", dialog.summary_label.text())
        dialog.close()
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_large_chapter_snapshot_refresh_replaces_rows_without_stale_checks(self):
        dialog = LibraryChapterDialog("123", "规模测试")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        first = tuple(
            _chapter(str(300 + index), index)
            for index in range(1, 201)
        )
        dialog.set_snapshots(first)
        dialog._checks["301"].setChecked(True)

        replacement = tuple(reversed(first[50:]))
        dialog.set_snapshots(replacement)

        self.assertEqual(dialog.table.rowCount(), 150)
        self.assertNotIn("301", dialog._checks)
        self.assertEqual(
            set(dialog._checks),
            {value.photo_id for value in replacement},
        )
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()
if __name__ == "__main__":
    unittest.main()
