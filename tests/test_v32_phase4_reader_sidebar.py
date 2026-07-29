import os
import tempfile
import unittest
from pathlib import Path


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QToolButton

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.pages.reader_page import ReaderPage
from jm_downloader.qt.theme import ThemeManager, load_stylesheet


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError

    async def close(self):
        return True


class ReaderSidebarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.controller = ReaderController(
            IdleReaderService(),
            result_interval_ms=5,
        )
        self.page = ReaderPage(self.controller)
        self.page.resize(760, 520)
        self.page.show()
        self.catalog = ChapterCatalogSnapshot(
            "123",
            "测试漫画",
            (
                ChapterSnapshot("301", 1, "第一章"),
                ChapterSnapshot("302", 2, "第二章"),
            ),
        )
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.shutdown(2.0)
        self.app.processEvents()
        self.temporary.cleanup()

    def _publish_chapter(self, pages=12):
        self.page._catalog = self.catalog
        self.page._generation = 7
        self.page._on_chapter_ready(
            7,
            ReaderChapterSnapshot("301", 1, "第一章", pages),
            tuple(
                ReaderPageSnapshot(
                    "301",
                    number,
                    pages,
                    ReaderPageState.PLACEHOLDER,
                )
                for number in range(1, pages + 1)
            ),
        )
        self.app.processEvents()

    def test_header_contains_only_title_and_close_control(self):
        buttons = self.page.header_widget.findChildren(
            QToolButton,
            options=Qt.FindChildOption.FindDirectChildrenOnly,
        )

        self.assertEqual(buttons, [self.page.back_button])
        self.assertIs(self.page.title_label.parent(), self.page.header_widget)
        self.assertIs(self.page.view.parent(), self.page.body_widget)
        self.assertIs(self.page.sidebar.parent(), self.page.body_widget)

    def test_vertical_slider_has_page_one_at_top_and_drag_is_not_overwritten(self):
        self._publish_chapter()
        slider = self.page.page_slider

        self.assertEqual(slider.orientation(), Qt.Orientation.Vertical)
        self.assertTrue(slider.invertedAppearance())
        self.assertTrue(slider.invertedControls())
        slider.setSliderDown(True)
        slider.setSliderPosition(8)
        self.page._preview_slider(8)
        self.page._update_page_display(2)

        self.assertEqual(slider.sliderPosition(), 8)
        self.assertEqual(self.page.page_label.text(), "8 / 12")
        slider.setSliderDown(False)
        self.page._apply_slider()
        self.assertEqual(self.page.current_page, 8)

    def test_sidebar_order_keeps_page_navigation_at_absolute_bottom(self):
        for theme in ("light", "dark"):
            ThemeManager(theme).apply()
            for scale in (1.0, 1.25, 1.5, 2.0):
                self.page.resize(
                    max(760, int(760 * scale)),
                    max(520, int(520 * scale)),
                )
                self.app.processEvents()
                self.assertLessEqual(
                    self.page.page_slider.geometry().bottom(),
                    self.page.previous_chapter_button.geometry().top(),
                )
                self.assertLessEqual(
                    self.page.previous_chapter_button.geometry().bottom(),
                    self.page.previous_page_button.geometry().top(),
                )
                self.assertFalse(
                    self.page.previous_page_button.geometry().intersects(
                        self.page.next_page_button.geometry()
                    )
                )
                self.assertLessEqual(
                    self.page.previous_page_button.geometry().bottom(),
                    self.page.sidebar.contentsRect().bottom(),
                )

    def test_both_themes_define_vertical_green_slider(self):
        for theme in ("light", "dark"):
            stylesheet = load_stylesheet(theme)
            self.assertIn(
                "QSlider#readerPageSlider::groove:vertical",
                stylesheet,
            )
            self.assertIn(
                "QSlider#readerPageSlider::handle:vertical",
                stylesheet,
            )
            self.assertNotIn(
                "QSlider#readerPageSlider::groove:horizontal",
                stylesheet,
            )


if __name__ == "__main__":
    unittest.main()
