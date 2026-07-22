import os
import unittest
from unittest.mock import Mock, patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QRadioButton, QWidget

from jm_downloader.models import (
    FavoriteFolderSnapshot,
    FavoriteItemSnapshot,
    FavoritesSnapshot,
)
from jm_downloader.qt.widgets.favorite_folder_dialogs import (
    FavoriteFolderManagerDialog,
    FavoriteTargetDialog,
)
from jm_downloader.qt.theme import Theme, load_stylesheet


class FakeManagerController(QObject):
    snapshot_changed = Signal(object)
    busy_changed = Signal(bool, str)
    mutation_failed = Signal(str, str, str)
    mutation_refresh_failed = Signal(str, str, str)

    def __init__(self, snapshot):
        super().__init__()
        self.current_snapshot = snapshot
        self.is_busy = False
        self.create_folder = Mock(return_value=1)
        self.delete_folder = Mock(return_value=2)


class FavoriteFolderDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["favorite-folder-dialog-tests"]
        )

    def setUp(self):
        self.snapshot = FavoritesSnapshot(
            "2026-07-22T12:00:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "全部收藏",
                    (FavoriteItemSnapshot("1", "One"),),
                ),
                FavoriteFolderSnapshot("8", "Empty", ()),
                FavoriteFolderSnapshot(
                    "9",
                    "Reading",
                    (FavoriteItemSnapshot("2", "Two"),),
                ),
            ),
        )

    def test_target_dialog_defaults_to_uncategorized_and_is_single_select(self):
        dialog = FavoriteTargetDialog(self.snapshot.folders)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        options = dialog.findChildren(QRadioButton)

        self.assertEqual(dialog.selected_folder_id, "0")
        self.assertEqual(len(options), 3)
        options[2].click()
        self.assertEqual(dialog.selected_folder_id, "9")
        self.assertEqual(sum(option.isChecked() for option in options), 1)
        dialog.close()

    def test_target_dialog_canvas_follows_both_themes(self):
        previous_stylesheet = self.app.styleSheet()
        samples = {}
        try:
            for theme in (Theme.LIGHT, Theme.DARK):
                self.app.setStyleSheet(load_stylesheet(theme))
                dialog = FavoriteTargetDialog(self.snapshot.folders)
                dialog.setAttribute(
                    Qt.WidgetAttribute.WA_DontShowOnScreen,
                    True,
                )
                dialog.show()
                self.app.processEvents()
                canvas = dialog.findChild(QWidget, "favoriteTargetCanvas")
                self.assertIsNotNone(canvas)
                image = canvas.grab().toImage()
                samples[theme] = image.pixelColor(
                    image.width() - 2,
                    image.height() - 2,
                )
                dialog.close()
        finally:
            self.app.setStyleSheet(previous_stylesheet)
            self.app.processEvents()

        self.assertGreater(samples[Theme.LIGHT].lightness(), 200)
        self.assertLess(samples[Theme.DARK].lightness(), 60)

    def test_manager_creates_and_only_deletes_empty_custom_folder(self):
        controller = FakeManagerController(self.snapshot)
        dialog = FavoriteFolderManagerDialog(controller)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        self.app.processEvents()

        dialog.folder_list.setCurrentRow(0)
        self.assertFalse(dialog.delete_button.isEnabled())
        dialog.folder_list.setCurrentRow(2)
        self.assertFalse(dialog.delete_button.isEnabled())
        dialog.folder_list.setCurrentRow(1)
        self.assertTrue(dialog.delete_button.isEnabled())

        dialog.name_input.setText("  Later   List  ")
        dialog.create_button.click()
        controller.create_folder.assert_called_once_with("Later List")

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            dialog.delete_button.click()
        controller.delete_folder.assert_not_called()
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.delete_button.click()
        controller.delete_folder.assert_called_once_with("8")
        dialog.close()
        controller.deleteLater()


if __name__ == "__main__":
    unittest.main()
