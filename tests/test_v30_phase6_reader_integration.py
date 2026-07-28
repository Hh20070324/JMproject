import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.pages.favorites_page import FavoritesPage
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.search_result_card import SearchResultCard
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    def protect(self, plaintext):
        return b"phase6:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"phase6:"):
            raise ValueError
        return ciphertext.removeprefix(b"phase6:")[::-1]


class SingleChapterReaderService:
    async def fetch_catalog(self, album_id):
        return make_catalog(album_id)

    async def load_chapter(self, catalog, photo_id):
        chapter = next(
            item for item in catalog.chapters
            if item.photo_id == photo_id
        )
        return (
            ReaderChapterSnapshot(
                chapter.photo_id,
                chapter.index,
                chapter.title,
                1,
            ),
            (
                ReaderPageSnapshot(
                    chapter.photo_id,
                    1,
                    1,
                    ReaderPageState.PLACEHOLDER,
                ),
            ),
        )

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("integration test must not fetch real pages")

    async def close(self):
        return True


def make_catalog(album_id="100") -> ChapterCatalogSnapshot:
    return ChapterCatalogSnapshot(
        album_id,
        "测试漫画",
        (ChapterSnapshot("301", 1, "第一章"),),
    )


class ReaderEntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-phase6-tests"]
        )

    def tearDown(self):
        for widget in tuple(self.app.topLevelWidgets()):
            if isinstance(widget, (DownloadPage, FavoritesPage)):
                widget.dispose()
                widget.close()
                widget.deleteLater()
        self.app.processEvents()

    def test_card_reading_is_primary_and_independent_from_download(self):
        card = SearchResultCard(
            SearchResultSnapshot("100", "测试漫画")
        )
        requests = []
        card.read_requested.connect(requests.append)
        card.set_reading_available(True)
        card.set_action_available(False)

        self.assertTrue(card.read_button.isEnabled())
        self.assertFalse(card.action_button.isEnabled())
        self.assertEqual(card.read_button.text(), "在线阅读")
        card.read_button.click()
        self.assertEqual(requests, ["100"])
        card.close()

    def test_search_and_favorites_emit_the_same_reader_contract(self):
        request = SearchRequest(SearchMode.EXACT_ID, "100")
        snapshot = SearchPageSnapshot(
            request,
            1,
            1,
            (
                SearchResultSnapshot(
                    "100",
                    "测试漫画",
                    chapter_catalog=make_catalog(),
                ),
            ),
        )
        search_page = DownloadPage(reader_available=True)
        search_events = []
        search_page.read_requested.connect(
            lambda item, source: search_events.append((item, source))
        )
        search_page._on_search_submitted(1, request)
        search_page._on_search_results(1, snapshot, False)
        search_page.comic_cards[0].read_button.click()

        favorites_page = FavoritesPage(reader_available=True)
        favorite_events = []
        favorites_page.read_requested.connect(
            lambda item, source: favorite_events.append((item, source))
        )
        favorites_page._set_cards(
            (
                SimpleNamespace(
                    album_id="100",
                    title="测试漫画",
                    authors=(),
                    tags=(),
                ),
            )
        )
        favorites_page.favorite_cards[0].read_button.click()

        self.assertEqual(search_events[0][1], ReaderSource.EXACT_SEARCH)
        self.assertEqual(
            search_events[0][0].chapter_catalog,
            make_catalog(),
        )
        self.assertEqual(favorite_events[0][1], ReaderSource.FAVORITES)
        self.assertEqual(favorite_events[0][0].album_id, "100")

    def test_reading_history_only_opens_after_explicit_button_click(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ReaderHistoryStore(
                ProtectedStore.reading_history(
                    AppPaths(Path(temporary)),
                    DeterministicProtector(),
                )
            )
            page = DownloadPage(
                reader_history_store=store,
                reader_available=True,
            )
            self.assertTrue(page.reading_history_button.isEnabled())
            requests = []
            page.reading_history_requested.connect(
                lambda: requests.append(True)
            )
            page.general_search_input.setFocus()
            self.app.processEvents()
            self.assertEqual(requests, [])
            page.reading_history_button.click()
            self.assertEqual(requests, [True])
            page.dispose()
            page.close()


class ReaderMainWindowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-main-window-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = AppPaths(Path(self.temporary.name))
        self.history = ReaderHistoryStore(
            ProtectedStore.reading_history(
                paths,
                DeterministicProtector(),
            )
        )
        self.controller = ReaderController(
            SingleChapterReaderService(),
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
        self.window.close()
        self.controller.shutdown(timeout=2.0)
        self.window.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_reader_is_non_modal_and_close_preserves_source_page(self):
        source_page = self.window.page("downloads")
        source_page.general_search_input.setText("保留搜索状态")
        snapshot = SearchResultSnapshot(
            "100",
            "测试漫画",
            chapter_catalog=make_catalog(),
        )

        self.window._open_reader(snapshot, ReaderSource.SEARCH)
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "downloads")
        self.assertTrue(self.window.reader_window.isVisible())
        self.assertFalse(self.window.reader_window.isModal())
        self.assertTrue(self.window.isEnabled())
        self.assertIs(self.window.page("downloads"), source_page)
        self.window.page("reader").back_button.click()
        self.app.processEvents()

        self.assertEqual(self.window.current_page, "downloads")
        self.assertFalse(self.window.reader_window.isVisible())
        self.assertEqual(
            source_page.general_search_input.text(),
            "保留搜索状态",
        )

    def test_download_current_chapter_uses_formal_task_controller(self):
        calls = []

        class FormalDownload:
            def add_task_batch(
                self,
                album_id,
                selected_chapter_ids,
                force_redownload_chapter_ids=(),
            ):
                calls.append(
                    (
                        album_id,
                        tuple(selected_chapter_ids),
                        tuple(force_redownload_chapter_ids),
                    )
                )
                return SimpleNamespace(
                    snapshots=(SimpleNamespace(id="task"),),
                    issues=(),
                    error=None,
                )

        self.window.download_controller = FormalDownload()
        self.window.page("reader")._catalog = make_catalog()
        self.window._download_reader_chapter("301")

        self.assertEqual(calls, [("100", ("301",), ())])
        self.assertIn(
            "正式下载任务",
            self.window.page("reader").error_banner.text(),
        )
        self.window.download_controller = None


if __name__ == "__main__":
    unittest.main()
