from pathlib import Path
import os
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.models import LibraryItem, LibraryLayout
from jm_downloader.qt.widgets.library_item_card import LibraryItemCard


class V321LibraryCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v321-library-card-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        images = root / "Pictures" / "123"
        packages = root / "PDFs" / "123"
        images.mkdir(parents=True)
        packages.mkdir(parents=True)
        self.item = LibraryItem(
            album_id="123",
            title="测试漫画",
            layout=LibraryLayout.MANAGED,
            chapter_count=2,
            image_count=20,
            image_size=2048,
            preview_path=None,
            pdf_directory=packages,
            pdf_size=1024,
            cbz_directory=packages,
            cbz_size=1024,
        )
        self.card = LibraryItemCard(self.item)
        self.card.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.card.resize(650, 164)
        self.card.show()
        self.app.processEvents()

    def tearDown(self):
        self.card.close()
        self.card.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_actions_use_the_approved_order_text_and_existing_signals(self):
        self.card.resize(self.card.minimumSizeHint().width(), 164)
        self.app.processEvents()
        self.assertEqual(self.card.height(), 164)
        self.assertEqual(self.card.open_images_button.text(), "图片")
        self.assertEqual(self.card.open_pdf_button.text(), "打包")
        self.assertEqual(self.card.read_button.text(), "完整阅读")
        self.assertEqual(
            self.card.read_button.toolButtonStyle(),
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
        )

        secondary = [
            self.card.view_task_button,
            self.card.chapter_button,
            self.card.delete_button,
        ]
        primary = [
            self.card.open_images_button,
            self.card.open_pdf_button,
            self.card.read_button,
        ]
        for group in (secondary, primary):
            self.assertTrue(all(button.isVisible() for button in group))
            self.assertEqual(
                [button.x() for button in group],
                sorted(button.x() for button in group),
            )
            for index, button in enumerate(group):
                for other in group[index + 1 :]:
                    self.assertFalse(
                        button.geometry().intersects(other.geometry())
                    )
        self.assertLess(
            secondary[-1].mapTo(self.card, secondary[-1].rect().topRight()).x(),
            primary[0].mapTo(self.card, primary[0].rect().topLeft()).x(),
        )

        opened = []
        read = []
        located = []
        chapter = []
        self.card.open_requested.connect(
            lambda *args: opened.append(args)
        )
        self.card.read_requested.connect(read.append)
        self.card.view_task_requested.connect(located.append)
        self.card.chapter_action_requested.connect(
            lambda *args: chapter.append(args)
        )

        self.card.open_images_button.click()
        self.card.open_pdf_button.click()
        self.card.read_button.click()
        self.card.view_task_button.click()
        self.card.chapter_button.click()

        self.assertEqual(
            opened,
            [("123", "images"), ("123", "package")],
        )
        self.assertEqual(read, ["123"])
        self.assertEqual(located, ["123"])
        self.assertEqual(chapter, [("123", "manage")])

    def test_hidden_primary_actions_reclaim_space(self):
        original_width = self.card.minimumSizeHint().width()
        package_only = LibraryItem(
            album_id="123",
            title="测试漫画",
            layout=LibraryLayout.UNVERIFIED,
            chapter_count=0,
            image_count=0,
            image_size=0,
            preview_path=None,
            pdf_directory=self.item.pdf_directory,
            pdf_size=1024,
            cbz_directory=None,
            cbz_size=0,
        )

        self.card.update_item(package_only)
        self.app.processEvents()

        self.assertFalse(self.card.open_images_button.isVisible())
        self.assertFalse(self.card.read_button.isVisible())
        self.assertTrue(self.card.open_pdf_button.isVisible())
        self.assertLess(
            self.card.minimumSizeHint().width(),
            original_width,
        )

    def test_both_themes_define_complete_read_button_states(self):
        resources = (
            Path(__file__).resolve().parents[1]
            / "jm_downloader"
            / "qt"
            / "resources"
        )
        for filename in ("styles_light.qss", "styles_dark.qss"):
            stylesheet = (resources / filename).read_text(encoding="utf-8")
            for selector in (
                "QToolButton#libraryReadButton {",
                "QToolButton#libraryReadButton:hover {",
                "QToolButton#libraryReadButton:pressed {",
                "QToolButton#libraryReadButton:focus {",
                "QToolButton#libraryReadButton:disabled {",
            ):
                self.assertIn(selector, stylesheet)
            self.assertIn("QWidget#libraryPrimaryActions", stylesheet)


if __name__ == "__main__":
    unittest.main()
