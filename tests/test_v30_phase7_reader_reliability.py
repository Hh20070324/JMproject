import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.account import AccountService
from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.reader_graphics_view import ReaderGraphicsView
from jm_downloader.qt.widgets.search_result_card import SearchResultCard
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    def protect(self, plaintext):
        return b"phase7:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"phase7:"):
            raise ValueError
        return ciphertext.removeprefix(b"phase7:")[::-1]


class IdleReaderService:
    async def fetch_catalog(self, album_id):
        return ChapterCatalogSnapshot(
            album_id,
            "可靠性测试",
            (ChapterSnapshot("301", 1, "第一章"),),
        )

    async def load_chapter(self, catalog, photo_id):
        return (
            ReaderChapterSnapshot(photo_id, 1, "第一章", 1),
            (
                ReaderPageSnapshot(
                    photo_id,
                    1,
                    1,
                    ReaderPageState.PLACEHOLDER,
                ),
            ),
        )

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("reliability test must stay offline")

    async def close(self):
        return True


def make_pages(count=2_000):
    return tuple(
        ReaderPageSnapshot(
            "301",
            page,
            count,
            ReaderPageState.PLACEHOLDER,
        )
        for page in range(1, count + 1)
    )


class ReaderScaleAndMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-phase7-scale-tests"]
        )

    def test_two_thousand_page_release_only_visits_loaded_pixels(self):
        view = ReaderGraphicsView()
        view.resize(900, 700)
        view.show()
        view.set_pages(make_pages())
        image = QImage(32, 64, QImage.Format.Format_RGB32)
        image.fill(0xFF2E7D57)
        for page in range(995, 1001):
            view.set_page_ready(
                ReaderPageSnapshot(
                    "301",
                    page,
                    2_000,
                    ReaderPageState.READY,
                    width=32,
                    height=64,
                    cache_path=Path(f"{page}.jpg"),
                ),
                image,
            )
        expected_max = image.sizeInBytes() * 6
        self.assertLessEqual(view.loaded_image_bytes, expected_max)

        started = time.monotonic()
        for page in range(1, 2_001, 5):
            view.scroll_to_page(page)
            view.release_far_pages(page)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertLessEqual(view.loaded_image_bytes, expected_max)
        view.close()

    def test_failed_page_does_not_release_successful_neighbor(self):
        view = ReaderGraphicsView()
        view.resize(800, 600)
        view.show()
        view.set_pages(make_pages(3))
        image = QImage(32, 64, QImage.Format.Format_RGB32)
        image.fill(0xFF2E7D57)
        view.set_page_ready(
            ReaderPageSnapshot(
                "301",
                1,
                3,
                ReaderPageState.READY,
                width=32,
                height=64,
                cache_path=Path("one.jpg"),
            ),
            image,
        )
        ready_bytes = view.loaded_image_bytes
        view.set_page_failed(2, "图片损坏")

        self.assertEqual(view.loaded_image_bytes, ready_bytes)
        self.assertFalse(view._pages[0].pixmap.pixmap().isNull())
        self.assertIn("图片损坏", view._pages[1].message.text())
        view.close()

    def test_card_actions_do_not_overlap_in_both_themes(self):
        manager = ThemeManager()
        for theme in ("light", "dark"):
            manager.set_theme(theme)
            card = SearchResultCard(
                SearchResultSnapshot("100", "测试漫画")
            )
            card.set_reading_available(True)
            card.set_favorite_visible(True)
            card.set_move_favorite_visible(True)
            card.show()
            self.app.processEvents()

            read_top = card.read_button.mapTo(card, QPoint(0, 0)).y()
            action_top = card.action_button.mapTo(card, QPoint(0, 0)).y()
            self.assertLessEqual(
                read_top + card.read_button.height(),
                action_top,
            )
            self.assertFalse(
                card.favorite_button.geometry().intersects(
                    card.move_button.geometry()
                )
            )
            card.close()


class ReaderCrossTrackReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-phase7-cross-track-tests"]
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
        self.controller = ReaderController(
            IdleReaderService(),
            history_store=self.history,
            result_interval_ms=5,
        )
        self.window = MainWindow(
            ThemeManager("light"),
            reader_controller=self.controller,
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
        self.window.download_controller = None
        self.window._shutdown_pending = False
        self.window._shutdown_complete = True
        self.window.close()
        self.controller.shutdown(timeout=2.0)
        self.app.processEvents()
        self.temporary.cleanup()

    def test_rejected_download_shutdown_keeps_reader_alive(self):
        begin_calls = []
        active_download = SimpleNamespace(
            has_active_tasks=lambda: True,
            begin_shutdown=lambda timeout: begin_calls.append(timeout),
        )
        self.window.download_controller = active_download

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.window.close()
        self.assertFalse(self.controller._shutdown_requested)
        self.assertEqual(begin_calls, [])

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.close()
        self.assertTrue(self.controller._shutdown_requested)
        self.assertEqual(begin_calls, [5.0])

    def test_sidebar_navigation_keeps_non_modal_reader_session(self):
        catalog = ChapterCatalogSnapshot(
            "100",
            "测试漫画",
            (ChapterSnapshot("301", 1, "第一章"),),
        )
        self.window._open_reader(
            SearchResultSnapshot(
                "100",
                "测试漫画",
                chapter_catalog=catalog,
            ),
            ReaderSource.SEARCH,
        )
        generation = self.controller.generation
        self.window.select_page("settings")

        self.assertEqual(self.window.current_page, "settings")
        self.assertEqual(self.controller.generation, generation)
        self.assertTrue(self.window.reader_window.isVisible())

    def test_logout_preserves_encrypted_reading_history(self):
        self.history.record(
            album_id="100",
            title="测试漫画",
            photo_id="301",
            chapter_title="第一章",
            chapter_index=1,
            page_number=7,
            page_count=20,
            source=ReaderSource.SEARCH,
        )
        account = AccountService(
            self.paths,
            client_factory=lambda _cookies=None: None,
        )
        operation = account.prepare_logout()
        account.logout(operation)

        self.assertTrue(self.paths.reading_history_file.is_file())
        self.assertEqual(self.history.load()[0].page_number, 7)


if __name__ == "__main__":
    unittest.main()
