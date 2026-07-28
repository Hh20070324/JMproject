from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.reader_window import ReaderWindow
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    def protect(self, plaintext):
        return b"phase2:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"phase2:"):
            raise ValueError
        return ciphertext.removeprefix(b"phase2:")[::-1]


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError("catalog network is not expected")

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError("chapter network is not expected")

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("page network is not expected")

    async def close(self):
        return True


def catalog(album_id: str) -> ChapterCatalogSnapshot:
    return ChapterCatalogSnapshot(
        album_id,
        f"漫画 {album_id}",
        (ChapterSnapshot(f"{album_id}01", 1, "第一章"),),
    )


class V31ReaderWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-window-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.settings = SettingsController(SettingsStore(self.paths))
        self.history = ReaderHistoryStore(
            ProtectedStore.reading_history(
                self.paths,
                DeterministicProtector(),
            )
        )
        self.controller = ReaderController(
            IdleReaderService(),
            history_store=self.history,
            result_interval_ms=5,
        )

    def tearDown(self):
        self.controller.shutdown(timeout=2.0)
        self.controller.deleteLater()
        self.settings.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_window_is_non_modal_and_saves_normal_geometry_on_close(self):
        window = ReaderWindow(
            self.controller,
            settings_controller=self.settings,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.begin_session("100", "测试漫画")
        window.resize(780, 560)
        window.move(12, 18)
        self.app.processEvents()

        self.assertFalse(window.isModal())
        self.assertTrue(window.isVisible())
        window.close()
        self.app.processEvents()

        saved = SettingsStore(self.paths).load()
        self.assertEqual(
            (saved.reader_window_width, saved.reader_window_height),
            (780, 560),
        )
        self.assertEqual(
            (saved.reader_window_x, saved.reader_window_y),
            (12, 18),
        )
        self.assertIsNone(window.session_album_id)
        window.deleteLater()

    def test_escape_does_not_hide_or_end_the_active_session(self):
        window = ReaderWindow(
            self.controller,
            settings_controller=self.settings,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.begin_session("100", "测试漫画")
        self.app.processEvents()
        generation = self.controller.generation

        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.app.processEvents()

        self.assertTrue(window.isVisible())
        self.assertTrue(window.has_session)
        self.assertEqual(window.session_album_id, "100")
        self.assertEqual(self.controller.generation, generation)
        window.close()
        window.deleteLater()

    def test_offscreen_saved_geometry_is_clamped_to_an_available_screen(self):
        offscreen = replace(
            self.settings.settings,
            reader_window_width=900,
            reader_window_height=620,
            reader_window_x=99_999,
            reader_window_y=-99_999,
        )
        self.assertTrue(self.settings.save(offscreen))
        window = ReaderWindow(
            self.controller,
            settings_controller=self.settings,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.begin_session("100")
        self.app.processEvents()

        available = window.screen().availableGeometry()
        self.assertTrue(available.contains(window.geometry()))
        window.close()
        window.deleteLater()

    def test_main_window_reuses_one_reader_with_cancel_and_confirm(self):
        window = MainWindow(
            ThemeManager("light"),
            settings_controller=self.settings,
            reader_controller=self.controller,
            reader_history_store=self.history,
            persist_window_state=False,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        first = SearchResultSnapshot(
            "100",
            "漫画 100",
            chapter_catalog=catalog("100"),
        )
        second = SearchResultSnapshot(
            "200",
            "漫画 200",
            chapter_catalog=catalog("200"),
        )

        window._open_reader(first, ReaderSource.SEARCH)
        first_generation = self.controller.generation
        with patch.object(
            window.reader_window,
            "activate_session",
        ) as activate:
            window._open_reader(first, ReaderSource.SEARCH)
        activate.assert_called_once_with()
        self.assertEqual(self.controller.generation, first_generation)

        with patch.object(
            window,
            "_confirm_reader_reuse",
            return_value=False,
        ):
            window._open_reader(second, ReaderSource.SEARCH)
        self.assertEqual(window.reader_window.session_album_id, "100")
        self.assertEqual(self.controller.generation, first_generation)

        with patch.object(
            window,
            "_confirm_reader_reuse",
            return_value=True,
        ):
            window._open_reader(second, ReaderSource.FAVORITES)
        self.assertEqual(window.reader_window.session_album_id, "200")
        self.assertGreater(self.controller.generation, first_generation)
        self.assertIs(window.page("reader"), window.reader_window.page)

        window._shutdown_complete = True
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_reuse_confirmation_defaults_and_escape_to_cancel(self):
        window = MainWindow(
            ThemeManager("light"),
            reader_controller=self.controller,
            persist_window_state=False,
        )

        class FakeMessageBox:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            selection = "取消"
            instance = None

            def __init__(self, _parent):
                self.buttons = {}
                self.default = None
                self.escape = None
                FakeMessageBox.instance = self

            def setIcon(self, _icon):
                pass

            def setWindowTitle(self, _title):
                pass

            def setText(self, _text):
                pass

            def setInformativeText(self, _text):
                pass

            def addButton(self, text, _role):
                button = object()
                self.buttons[text] = button
                return button

            def setDefaultButton(self, button):
                self.default = button

            def setEscapeButton(self, button):
                self.escape = button

            def exec(self):
                pass

            def clickedButton(self):
                return self.buttons[self.selection]

        with patch(
            "jm_downloader.qt.main_window.QMessageBox",
            FakeMessageBox,
        ):
            self.assertFalse(window._confirm_reader_reuse("另一部漫画"))
            dialog = FakeMessageBox.instance
            self.assertIs(dialog.default, dialog.buttons["取消"])
            self.assertIs(dialog.escape, dialog.buttons["取消"])
            FakeMessageBox.selection = "切换阅读"
            self.assertTrue(window._confirm_reader_reuse("另一部漫画"))

        window._shutdown_complete = True
        window.close()
        window.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
