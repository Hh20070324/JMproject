import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QToolButton

from jm_downloader.models import (
    ChapterImageStatus,
    ChapterPackageStatus,
    LibraryChapterSnapshot,
    LibraryItem,
    LibraryLayout,
)
from jm_downloader.qt.pages import LibraryPage
from jm_downloader.qt.widgets.library_chapter_dialogs import (
    LibraryChapterDialog,
    PackageFormatConfirmationDialog,
)


def chapter(
    photo_id="301",
    *,
    package_format="pdf",
    image_status=ChapterImageStatus.COMPLETE,
    package_status=ChapterPackageStatus.MISSING,
):
    return LibraryChapterSnapshot(
        album_id="123",
        photo_id=photo_id,
        index=int(photo_id) - 300,
        title=f"章节 {photo_id}",
        image_directory=Path(f"Pictures/123/title/{photo_id}"),
        package_path=None,
        page_count=2,
        valid_image_count=(
            2 if image_status is ChapterImageStatus.COMPLETE else 1
        ),
        image_status=image_status,
        package_format=package_format,
        package_status=package_status,
        downloaded_at_utc="2026-07-27T08:00:00Z",
        can_rebuild=(
            image_status is ChapterImageStatus.COMPLETE
            and package_format in {"pdf", "cbz"}
        ),
        can_redownload=image_status is not ChapterImageStatus.COMPLETE,
        can_delete_images=True,
        can_delete_package=package_format in {"pdf", "cbz"},
        can_delete_all=True,
        problem_codes=("package_missing",),
    )


def item(layout=LibraryLayout.MANAGED):
    return LibraryItem(
        album_id="123",
        title="章节管理测试",
        layout=layout,
        chapter_count=2,
        image_count=4,
        image_size=100,
        preview_path=None,
        pdf_directory=None,
        pdf_size=0,
    )


class FakeLibraryController(QObject):
    items_reset = Signal(object)
    loading_changed = Signal(bool)
    busy_albums_changed = Signal(object)
    active_albums_changed = Signal(object)
    command_failed = Signal(str, str, str)
    batch_delete_finished = Signal(str, object, object)
    request_completed = Signal(int, str, str, object)
    request_failed = Signal(int, str, str, str)

    def __init__(self, library_item):
        super().__init__()
        self.item = library_item
        self.requests = []
        self.next_request = 0

    def list_items(self):
        return [self.item]

    def active_album_ids(self):
        return frozenset()

    def busy_album_ids(self):
        return frozenset()

    def has_pending_mutations(self):
        return False

    def refresh(self):
        pass

    def check_chapters(self, album_id):
        self.next_request += 1
        self.requests.append(("check", album_id, self.next_request))
        return self.next_request

    def open_item(self, *_args):
        pass

    def delete_item(self, *_args):
        pass

    def batch_delete(self, *_args):
        return True


class V291Phase6UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v291-phase6-ui-tests"]
        )

    def test_managed_card_opens_modeless_dialog_and_drops_late_result(self):
        controller = FakeLibraryController(item())
        page = LibraryPage(controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.resize(1000, 700)
        page.show()
        self.app.processEvents()

        card = page.item_card("123")
        self.assertTrue(card.chapter_button.isVisible())
        self.assertEqual(card.chapter_button.toolTip(), "管理章节")
        card.chapter_button.click()
        self.app.processEvents()

        dialog = page._chapter_dialogs["123"]
        request_id = controller.requests[-1][2]
        self.assertFalse(dialog.isModal())
        self.assertFalse(dialog.table.isEnabled())
        dialog.close()
        self.app.processEvents()
        controller.request_completed.emit(
            request_id,
            "check_chapters",
            "123",
            (chapter(),),
        )
        self.app.processEvents()
        self.assertNotIn(request_id, page._chapter_requests)
        self.assertNotIn("123", page._chapter_dialogs)

        page.close()
        page.deleteLater()
        controller.deleteLater()
        self.app.processEvents()

    def test_chapter_dialog_uses_instant_popup_and_scales_without_overlap(self):
        dialog = LibraryChapterDialog("123", "很长的章节管理测试漫画名称")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.set_snapshots(
            (
                chapter("301"),
                chapter(
                    "302",
                    package_format="images",
                    package_status=ChapterPackageStatus.NOT_APPLICABLE,
                ),
            )
        )
        dialog.show()
        for width, height in (
            (960, 560),
            (1200, 700),
            (1440, 840),
            (1920, 1120),
        ):
            dialog.resize(width, height)
            self.app.processEvents()
            self.assertGreater(dialog.table.viewport().width(), 0)
            self.assertGreater(dialog.table.viewport().height(), 0)
            self.assertFalse(
                dialog.repair_button.geometry().intersects(
                    dialog.recheck_button.geometry()
                )
            )
        action = dialog.table.cellWidget(0, 7)
        self.assertIsInstance(action, QToolButton)
        self.assertEqual(
            action.popupMode(),
            QToolButton.ToolButtonPopupMode.InstantPopup,
        )
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()

    def test_unknown_format_confirmation_defaults_to_current_setting(self):
        unknown = chapter(
            package_format=None,
            package_status=ChapterPackageStatus.UNKNOWN,
        )
        dialog = PackageFormatConfirmationDialog((unknown,), "cbz")
        self.assertTrue(dialog.buttons["cbz"].isChecked())
        self.assertFalse(dialog.buttons["pdf"].isChecked())
        dialog.close()
        dialog.deleteLater()

    def test_legacy_identification_requires_explicit_network_confirmation(self):
        controller = FakeLibraryController(item(LibraryLayout.LEGACY))
        page = LibraryPage(controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        card = page.item_card("123")
        self.assertEqual(
            card.chapter_button.toolTip(),
            "识别章节（可能访问网络）",
        )
        with patch(
            "jm_downloader.qt.pages.library_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            card.chapter_button.click()
        self.assertEqual(page._catalog_requests, {})
        page.close()
        page.deleteLater()
        controller.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
