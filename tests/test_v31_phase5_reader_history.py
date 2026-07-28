import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.models import AccountSnapshot, AccountStatus, ReaderSource
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.reader_history_dialog import (
    ReaderHistoryDialog,
)
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    def protect(self, plaintext):
        return b"phase5:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"phase5:"):
            raise ValueError
        return ciphertext.removeprefix(b"phase5:")[::-1]


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError("history test must not fetch a catalog")

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError("history test must not load a chapter")

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("history test must not fetch a page")

    async def close(self):
        return True


class V31SharedReaderHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-shared-reader-history-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.history = ReaderHistoryStore(
            ProtectedStore.reading_history(
                self.paths,
                DeterministicProtector(),
            )
        )
        self.reader = ReaderController(
            IdleReaderService(),
            history_store=self.history,
            result_interval_ms=5,
        )
        self.window = MainWindow(
            ThemeManager("light"),
            reader_controller=self.reader,
            reader_history_store=self.history,
            persist_window_state=False,
        )
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.reader.shutdown(timeout=2.0)
        self.reader.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_search_and_signed_out_favorites_use_the_same_explicit_dialog(self):
        search = self.window.page("downloads")
        favorites = self.window.page("favorites")
        self.assertTrue(search.reading_history_button.isEnabled())
        self.assertTrue(favorites.reading_history_button.isEnabled())
        self.assertGreaterEqual(
            favorites._history_fallback_row.indexOf(
                favorites.reading_history_button
            ),
            0,
        )

        with patch.object(
            ReaderHistoryDialog,
            "exec",
            return_value=ReaderHistoryDialog.DialogCode.Rejected,
        ) as execute:
            search.general_search_input.setFocus()
            self.app.processEvents()
            execute.assert_not_called()

            search.reading_history_button.click()
            self.window.select_page("favorites")
            favorites.reading_history_button.click()

        self.assertEqual(execute.call_count, 2)
        self.assertTrue(favorites.reading_history_button.isVisible())

    def test_signed_in_favorites_history_joins_the_filter_toolbar(self):
        favorites = self.window.page("favorites")

        self.window.select_page("favorites")
        favorites._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.app.processEvents()

        self.assertGreaterEqual(
            favorites._favorites_filter_row.indexOf(
                favorites.reading_history_button
            ),
            0,
        )
        self.assertEqual(
            favorites.reading_history_button.height(),
            favorites.folder_button.height(),
        )
        self.assertEqual(
            favorites.reading_history_button.mapToGlobal(
                favorites.reading_history_button.rect().center()
            ).y(),
            favorites.folder_button.mapToGlobal(
                favorites.folder_button.rect().center()
            ).y(),
        )

    def test_selected_entry_from_either_button_uses_shared_reader_entry(self):
        entry = self.history.record(
            album_id="100",
            title="测试漫画",
            photo_id="301",
            chapter_title="第一章",
            chapter_index=1,
            page_number=8,
            page_count=20,
            source=ReaderSource.SEARCH,
        )[0]

        with (
            patch.object(
                ReaderHistoryDialog,
                "exec",
                return_value=ReaderHistoryDialog.DialogCode.Accepted,
            ),
            patch.object(
                ReaderHistoryDialog,
                "selected_entry",
                return_value=entry,
            ),
            patch.object(
                self.window,
                "_open_reader_history",
            ) as open_history,
        ):
            self.window.page("downloads").reading_history_button.click()
            self.window.page("favorites").reading_history_button.click()

        self.assertEqual(open_history.call_count, 2)
        open_history.assert_any_call(entry)

    def test_history_buttons_disable_when_no_history_store_exists(self):
        reader = ReaderController(
            IdleReaderService(),
            result_interval_ms=5,
        )
        window = MainWindow(
            ThemeManager("dark"),
            reader_controller=reader,
            persist_window_state=False,
        )
        window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        try:
            self.assertFalse(
                window.page("downloads").reading_history_button.isEnabled()
            )
            self.assertFalse(
                window.page("favorites").reading_history_button.isEnabled()
            )
        finally:
            window.close()
            window.deleteLater()
            reader.shutdown(timeout=2.0)
            reader.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
