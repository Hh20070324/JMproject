import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QToolButton

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.pages.reader_page import ReaderPage
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.reader_chapter_dialog import (
    ReaderChapterDialog,
)
from jm_downloader.qt.widgets.reader_graphics_view import (
    ReaderGraphicsView,
)
from jm_downloader.settings import AppPaths


def make_pages(count=100):
    return tuple(
        ReaderPageSnapshot(
            "301",
            page,
            count,
            ReaderPageState.PLACEHOLDER,
        )
        for page in range(1, count + 1)
    )


class ReaderGraphicsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-view-tests"]
        )

    def test_two_thousand_placeholders_use_discrete_page_navigation(self):
        view = ReaderGraphicsView()
        view.resize(900, 700)
        view.show()
        self.app.processEvents()

        started = time.monotonic()
        view.set_pages(make_pages(2000))
        elapsed = time.monotonic() - started
        view.scroll_to_page(1000)
        self.app.processEvents()

        self.assertEqual(view.page_count, 2000)
        self.assertLess(elapsed, 2.0)
        self.assertGreater(view.scene().sceneRect().height(), 1_000_000)
        self.assertIn(view.current_page(), {999, 1000, 1001})
        self.assertEqual(view.loaded_image_bytes, 0)
        view.close()

    def test_real_size_relayout_keeps_page_anchor_and_releases_far_pixels(self):
        view = ReaderGraphicsView()
        view.resize(800, 600)
        view.show()
        view.set_pages(make_pages(120))
        view.scroll_to_page(60)
        self.app.processEvents()
        before = view.current_page()

        image = QImage(600, 1200, QImage.Format.Format_RGB32)
        image.fill(0xFF2E7D57)
        snapshot = ReaderPageSnapshot(
            "301",
            1,
            120,
            ReaderPageState.READY,
            width=600,
            height=1200,
            cache_path=Path("controlled.png"),
        )
        view.set_page_ready(snapshot, image)
        self.app.processEvents()

        self.assertLessEqual(abs(view.current_page() - before), 1)
        view.scroll_to_page(1)
        view.set_page_ready(snapshot, image)
        self.assertGreater(view.loaded_image_bytes, 0)
        released = view.release_far_pages(100)
        self.assertIn(1, released)
        self.assertEqual(view.loaded_image_bytes, 0)
        view.close()

    def test_fit_width_has_no_page_gap_and_fit_page_uses_viewport_slots(self):
        view = ReaderGraphicsView()
        view.resize(800, 600)
        view.show()
        self.app.processEvents()
        view.set_pages(make_pages(2))
        image = QImage(600, 1200, QImage.Format.Format_RGB32)
        image.fill(0xFF2E7D57)
        for page_number in (1, 2):
            view.set_page_ready(
                ReaderPageSnapshot(
                    "301",
                    page_number,
                    2,
                    ReaderPageState.READY,
                    width=600,
                    height=1200,
                    cache_path=Path(f"{page_number}.png"),
                ),
                image,
            )

        self.assertEqual(
            view.page_top(2),
            view._pages[0].slot_height,
        )
        view.set_layout_mode("fit_page")

        first = view._pages[0]
        self.assertLessEqual(
            first.display_height,
            view.viewport().height(),
        )
        self.assertLess(first.display_width, view.target_width)
        self.assertEqual(first.slot_height, view.viewport().height())
        self.assertEqual(view.page_top(2), first.slot_height)
        view.close()


class FakeReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError

    async def close(self):
        return True


class ReaderPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-page-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.settings_controller = SettingsController(
            SettingsStore(self.paths)
        )
        self.controller = ReaderController(
            FakeReaderService(),
            result_interval_ms=5,
            memory_budget_bytes=1024,
        )
        self.page = ReaderPage(
            self.controller,
            settings_controller=self.settings_controller,
        )
        self.page.resize(900, 700)
        self.page.show()
        self.catalog = ChapterCatalogSnapshot(
            "123",
            "测试漫画",
            (
                ChapterSnapshot("301", 1, "第一章"),
                ChapterSnapshot("302", 2, "第二章"),
                ChapterSnapshot("303", 3, "第三章"),
            ),
        )

    def tearDown(self):
        self.controller.shutdown(1.0)
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_chapter_selector_is_tool_button_and_dialog_selects_once(self):
        self.assertIsInstance(self.page.chapter_button, QToolButton)
        dialog = ReaderChapterDialog(
            self.catalog,
            current_photo_id="301",
        )
        selected = []
        dialog.finished.connect(selected.append)
        dialog.show()
        self.app.processEvents()
        dialog.chapter_buttons[1].click()
        self.app.processEvents()

        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.selected_photo_id(), "302")
        self.assertEqual(selected, [QDialog.DialogCode.Accepted])

    def test_page_slider_and_navigation_share_same_page_definition(self):
        self.page._catalog = self.catalog
        self.page._generation = 7
        chapter = ReaderChapterSnapshot("302", 2, "第二章", 20)
        self.page._on_chapter_ready(7, chapter, make_pages(20))
        self.page.view.scroll_to_page(10)
        self.app.processEvents()
        self.page._on_viewport(10, (10,), self.page.view.target_width)

        self.assertEqual(self.page.page_slider.value(), 10)
        self.assertEqual(self.page.page_label.text(), "10 / 20")
        self.page._previous_page()
        self.app.processEvents()
        self.assertIn(self.page.view.current_page(), {8, 9, 10})
        self.page.page_slider.setValue(15)
        self.page._apply_slider()
        self.app.processEvents()
        self.assertIn(self.page.view.current_page(), {14, 15, 16})

    def test_slider_drag_is_not_overwritten_by_old_viewport_updates(self):
        self.page._catalog = self.catalog
        self.page._generation = 7
        chapter = ReaderChapterSnapshot("302", 2, "第二章", 20)
        self.page._on_chapter_ready(7, chapter, make_pages(20))
        self.page.page_slider.setValue(10)
        self.page.page_slider.setSliderDown(True)
        self.page.page_slider.setSliderPosition(15)
        self.page._preview_slider(15)

        self.page._on_viewport(
            10,
            (10,),
            self.page.view.target_width,
        )

        self.assertEqual(self.page.page_slider.sliderPosition(), 15)
        self.assertEqual(self.page.page_label.text(), "15 / 20")
        with patch.object(self.page.view, "scroll_to_page") as scroll:
            self.page._apply_slider()
        scroll.assert_called_once_with(15)
        self.page.page_slider.setSliderDown(False)

    def test_reader_layout_selector_persists_global_preference(self):
        self.assertIsInstance(self.page.layout_button, QToolButton)
        self.assertEqual(
            self.page.layout_button.popupMode(),
            QToolButton.ToolButtonPopupMode.InstantPopup,
        )

        self.page.layout_action("fit_page").trigger()

        self.assertEqual(self.page.view.layout_mode, "fit_page")
        self.assertEqual(
            self.page.layout_button.text(),
            "阅读视图：单页视图",
        )
        self.assertEqual(
            self.settings_controller.settings.reader_layout,
            "fit_page",
        )
        self.assertEqual(
            SettingsStore(self.paths).load().reader_layout,
            "fit_page",
        )

    def test_theme_and_scale_layouts_do_not_overlap_controls(self):
        manager = ThemeManager()
        for theme in ("light", "dark"):
            manager.set_theme(theme)
            for scale in (1.0, 1.25, 1.5, 2.0):
                self.page.resize(
                    max(760, int(760 * scale)),
                    max(520, int(520 * scale)),
                )
                self.app.processEvents()
                self.assertEqual(
                    self.page.page_slider.orientation(),
                    Qt.Orientation.Vertical,
                )
                self.assertFalse(
                    self.page.previous_page_button.geometry().intersects(
                        self.page.next_page_button.geometry()
                    )
                )
                self.assertLessEqual(
                    self.page.page_slider.geometry().bottom(),
                    self.page.previous_chapter_button.geometry().top(),
                )
                self.assertLessEqual(
                    self.page.previous_chapter_button.geometry().bottom(),
                    self.page.previous_page_button.geometry().top(),
                )


if __name__ == "__main__":
    unittest.main()
