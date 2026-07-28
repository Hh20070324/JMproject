import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QKeySequence, QWheelEvent
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.pages.reader_page import ReaderPage
from jm_downloader.qt.reader_window import ReaderWindow
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.qt.widgets.reader_graphics_view import ReaderGraphicsView
from jm_downloader.settings import AppPaths, READER_ZOOM_LEVELS


def make_pages(count: int = 12):
    return tuple(
        ReaderPageSnapshot(
            "301",
            page,
            count,
            ReaderPageState.PLACEHOLDER,
        )
        for page in range(1, count + 1)
    )


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError

    async def close(self):
        return True


class V31ReaderZoomViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-zoom-tests"]
        )

    def setUp(self):
        self.view = ReaderGraphicsView()
        self.view.resize(800, 600)
        self.view.show()
        self.app.processEvents()
        self.view.set_pages(make_pages())
        self.image = QImage(600, 1200, QImage.Format.Format_RGB32)
        self.image.fill(0xFF247A52)
        self.view.set_page_ready(
            ReaderPageSnapshot(
                "301",
                1,
                12,
                ReaderPageState.READY,
                width=600,
                height=1200,
                cache_path=Path("controlled.jpg"),
            ),
            self.image,
        )

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def test_all_six_zoom_levels_scale_both_layout_baselines(self):
        for mode in ("fit_width", "fit_page"):
            self.view.set_layout_mode(mode)
            self.view.set_zoom_percent(100)
            baseline_width = self.view._pages[0].display_width
            baseline_height = self.view._pages[0].display_height
            for percent in sorted(READER_ZOOM_LEVELS):
                with self.subTest(mode=mode, percent=percent):
                    self.view.set_zoom_percent(percent)
                    self.assertAlmostEqual(
                        self.view._pages[0].display_width,
                        baseline_width * percent / 100,
                        delta=1.0,
                    )
                    self.assertAlmostEqual(
                        self.view._pages[0].display_height,
                        baseline_height * percent / 100,
                        delta=1.0,
                    )

    def test_zoom_over_one_hundred_uses_real_horizontal_scroll(self):
        self.view.set_layout_mode("fit_width")
        self.view.set_zoom_percent(150)
        self.app.processEvents()

        self.assertGreater(
            self.view.content_width,
            self.view.viewport().width(),
        )
        self.assertGreater(self.view.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(self.view.target_width, 4096)

        self.view.set_zoom_percent(75)
        self.app.processEvents()
        self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)

    def test_zoom_keeps_page_and_relative_offset_anchor(self):
        self.view.scroll_to_page(7)
        self.app.processEvents()
        before = self.view.current_page()

        self.view.set_zoom_percent(150)
        self.app.processEvents()

        self.assertLessEqual(abs(self.view.current_page() - before), 1)

    def test_native_wheel_remains_continuous_in_both_layouts(self):
        for mode in ("fit_width", "fit_page"):
            with self.subTest(mode=mode):
                self.view.set_layout_mode(mode)
                self.view.set_zoom_percent(100)
                bar = self.view.verticalScrollBar()
                bar.setValue(0)
                event = QWheelEvent(
                    QPointF(20, 20),
                    QPointF(20, 20),
                    QPoint(),
                    QPoint(0, -120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.ScrollUpdate,
                    False,
                )
                QApplication.sendEvent(self.view.viewport(), event)
                self.app.processEvents()

                self.assertGreater(bar.value(), 0)
                self.assertLess(
                    bar.value(),
                    int(self.view._pages[0].slot_height),
                )

    def test_pixel_delta_is_not_quantized_to_a_whole_page(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(0)
        event = QWheelEvent(
            QPointF(20, 20),
            QPointF(20, 20),
            QPoint(0, -35),
            QPoint(),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(self.view.viewport(), event)
        self.app.processEvents()

        self.assertGreater(bar.value(), 0)
        self.assertLess(bar.value(), int(self.view._pages[0].slot_height))


class V31ReaderToolbarAndKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-toolbar-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.settings = SettingsController(SettingsStore(self.paths))
        self.controller = ReaderController(
            IdleReaderService(),
            result_interval_ms=5,
        )
        self.window = ReaderWindow(
            self.controller,
            settings_controller=self.settings,
            persist_geometry=False,
        )
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.window.resize(900, 700)
        self.window.begin_session("100", "测试漫画")
        self.page: ReaderPage = self.window.page
        self.page._catalog = ChapterCatalogSnapshot(
            "100",
            "测试漫画",
            (ChapterSnapshot("301", 1, "第一章"),),
        )
        self.page._chapter = ReaderChapterSnapshot(
            "301",
            1,
            "第一章",
            12,
        )
        self.page.view.set_pages(make_pages())
        self.page.view.scroll_to_page(6)
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.controller.shutdown(timeout=2.0)
        self.controller.deleteLater()
        self.settings.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_zoom_menu_persists_and_tracks_settings(self):
        self.page.zoom_action(150).trigger()

        self.assertEqual(self.page.view.zoom_percent, 150)
        self.assertEqual(self.settings.settings.reader_zoom_percent, 150)
        self.assertEqual(
            SettingsStore(self.paths).load().reader_zoom_percent,
            150,
        )
        self.assertEqual(self.page.zoom_button.text(), "缩放：150%")

        self.assertTrue(
            self.settings.save(
                replace(
                    self.settings.settings,
                    reader_zoom_percent=50,
                )
            )
        )
        self.app.processEvents()
        self.assertEqual(self.page.view.zoom_percent, 50)

    def test_shortcuts_map_to_page_navigation_without_up_or_down(self):
        shortcuts = {
            shortcut.key().toString(QKeySequence.SequenceFormat.PortableText):
            shortcut
            for shortcut in self.window._shortcuts
        }
        self.assertEqual(
            set(shortcuts),
            {"Left", "PgUp", "Shift+Space", "Right", "PgDown", "Space"},
        )
        self.assertNotIn("Up", shortcuts)
        self.assertNotIn("Down", shortcuts)

        with (
            patch.object(self.page.view, "current_page", return_value=6),
            patch.object(self.page.view, "scroll_to_page") as scroll,
        ):
            for sequence in ("Left", "PgUp", "Shift+Space"):
                shortcuts[sequence].activated.emit()
                scroll.assert_called_with(5)
            for sequence in ("Right", "PgDown", "Space"):
                shortcuts[sequence].activated.emit()
                scroll.assert_called_with(7)


if __name__ == "__main__":
    unittest.main()
