import os
import unittest
from pathlib import Path
from types import SimpleNamespace


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QToolButton

from jm_downloader import models
from jm_downloader.models import ChapterCatalogSnapshot, ChapterSnapshot
from jm_downloader.qt.chapter_download_flow import ChapterDownloadFlow
from jm_downloader.qt.pages.settings_page import SettingsPage
from jm_downloader.qt.theme import Theme, ThemeManager
from jm_downloader.qt.widgets.chapter_selection_dialog import (
    ChapterSelectionDialog,
)


def catalog(count=12):
    return ChapterCatalogSnapshot(
        "123",
        "章节上限测试",
        tuple(
            ChapterSnapshot(str(300 + index), index, f"章节 {index}")
            for index in range(1, count + 1)
        ),
    )


class DownloadController(QObject):
    def __init__(self):
        super().__init__()
        self.calls = []

    def add_task(self, album_id, selected_chapter_ids=None):
        self.calls.append((album_id, tuple(selected_chapter_ids or ())))
        return SimpleNamespace(album_id=album_id)


class OversizedDialog:
    def __init__(self, value, _parent=None):
        self.value = value

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_chapter_ids(self):
        return tuple(chapter.photo_id for chapter in self.value.chapters[:11])

    def deleteLater(self):
        pass


class ChapterSelectionBatchUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v28-chapter-contract-tests"]
        )

    def setUp(self):
        self.dialog = ChapterSelectionDialog(catalog())
        self.dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_select_all_has_no_ui_limit_and_reports_two_batches(self):
        self.assertEqual(models.MAX_CHAPTERS_PER_TASK, 10)

        self.dialog.select_all_checkbox.click()
        self.app.processEvents()

        checked = [
            index
            for index, checkbox in enumerate(
                self.dialog.chapter_checkboxes,
                start=1,
            )
            if checkbox.isChecked()
        ]
        self.assertEqual(checked, list(range(1, 13)))
        self.assertEqual(
            self.dialog.select_all_checkbox.checkState(),
            Qt.CheckState.Checked,
        )
        self.assertEqual(
            self.dialog.selection_summary.text(),
            "已选 12 章，将创建 2 个任务",
        )

        self.dialog.select_all_checkbox.click()
        self.app.processEvents()
        self.assertEqual(self.dialog.selected_chapter_ids(), ())

    def test_eleventh_choice_remains_selectable(self):
        boxes = self.dialog.chapter_checkboxes
        self.dialog.select_all_checkbox.click()
        boxes[10].click()
        self.app.processEvents()

        self.assertFalse(boxes[10].isChecked())
        boxes[10].click()
        self.app.processEvents()

        self.assertTrue(boxes[10].isChecked())
        self.assertEqual(len(self.dialog.selected_chapter_ids()), 12)

    def test_download_flow_splits_oversized_dialog_result(self):
        controller = DownloadController()
        flow = ChapterDownloadFlow(
            controller,
            dialog_factory=OversizedDialog,
        )
        failures = []
        flow.failed.connect(
            lambda album_id, message: failures.append((album_id, message))
        )

        flow.start("123", catalog())

        self.assertEqual(
            controller.calls,
            [
                ("123", tuple(str(value) for value in range(301, 311))),
                ("123", ("311",)),
            ],
        )
        self.assertEqual(failures, [])
        flow.dispose()
        flow.deleteLater()


class MultiChapterSettingsUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v28-settings-contract-tests"]
        )

    def setUp(self):
        self.theme_manager = ThemeManager(Theme.LIGHT)
        self.page = SettingsPage(self.theme_manager)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def test_behavior_selector_uses_toolbutton_instant_menu_and_default(self):
        button = self.page.multi_chapter_behavior_button
        menu = self.page.multi_chapter_behavior_menu

        self.assertIsInstance(button, QToolButton)
        self.assertIsInstance(menu, QMenu)
        self.assertIs(button.menu(), menu)
        self.assertEqual(
            button.popupMode(),
            QToolButton.ToolButtonPopupMode.InstantPopup,
        )
        actions = menu.actions()
        self.assertEqual(
            [action.data() for action in actions],
            ["parallel", "queued"],
        )
        self.assertTrue(all(action.isCheckable() for action in actions))
        self.assertTrue(actions[0].isChecked())
        self.assertIn("同时 2 章", button.text())

        actions[1].trigger()
        self.app.processEvents()
        self.assertTrue(actions[1].isChecked())
        self.assertFalse(actions[0].isChecked())
        self.assertIn("同时 1 章", button.text())


if __name__ == "__main__":
    unittest.main()
