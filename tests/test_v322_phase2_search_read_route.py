import os
import unittest
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterSnapshot,
    LocalReadProbeSnapshot,
    LocalReadProbeState,
    ReaderContentMode,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.search_result_card import SearchResultCard


class _ProbeController:
    def __init__(self):
        self.calls = []
        self.next_id = 41

    def probe_local_read(self, album_id):
        self.calls.append(album_id)
        return self.next_id

    def has_pending_mutations(self):
        return False


class V322SearchCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-search-route-tests"]
        )

    def test_read_button_uses_local_first_copy_and_blocks_duplicate_click(self):
        card = SearchResultCard(SearchResultSnapshot("123", "测试漫画"))
        card.set_reading_available(True)
        requests = []
        card.read_requested.connect(requests.append)

        self.assertEqual(card.read_button.text(), "阅读")
        self.assertIn("优先读取本地完整章节", card.read_button.toolTip())
        card.read_button.click()
        card.set_reading_checking(True)
        card.read_button.click()

        self.assertEqual(requests, ["123"])
        self.assertEqual(card.read_button.text(), "检查中…")
        self.assertFalse(card.read_button.isEnabled())

        card.set_reading_checking(False)
        self.assertEqual(card.read_button.text(), "阅读")
        self.assertTrue(card.read_button.isEnabled())
        card.close()

    def test_page_updates_all_duplicate_cards_without_storing_widget_context(self):
        page = DownloadPage(reader_available=True)
        first = SearchResultCard(SearchResultSnapshot("123", "甲"), page)
        second = SearchResultCard(SearchResultSnapshot("123", "乙"), page)
        first.set_reading_available(True)
        second.set_reading_available(True)
        page._cards_by_album = {"123": [first, second]}

        page.set_read_probe_busy("123", True)
        self.assertTrue(all(
            card.read_button.text() == "检查中…"
            for card in (first, second)
        ))

        page.set_read_probe_busy("123", False)
        self.assertTrue(all(
            card.read_button.text() == "阅读"
            for card in (first, second)
        ))
        page.dispose()
        page.close()


class V322SearchRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-main-window-route-tests"]
        )

    def setUp(self):
        self.window = MainWindow(
            ThemeManager("light"),
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.controller = _ProbeController()
        self.window.library_controller = self.controller
        self.snapshot = SearchResultSnapshot("123", "测试漫画")

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_normal_absence_opens_online_once_after_background_result(self):
        busy = []
        page = self.window.page("downloads")
        with patch.object(
            page,
            "set_read_probe_busy",
            side_effect=lambda album_id, value: busy.append(
                (album_id, value)
            ),
        ), patch.object(self.window, "_open_reader") as online:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.EXACT_SEARCH,
            )
            self.window._on_local_read_probe_completed(
                41,
                "probe_local_read",
                "123",
                LocalReadProbeSnapshot(
                    "123",
                    LocalReadProbeState.ABSENT,
                ),
            )

        self.assertEqual(self.controller.calls, ["123"])
        self.assertEqual(busy, [("123", True), ("123", False)])
        online.assert_called_once_with(
            self.snapshot,
            ReaderSource.EXACT_SEARCH,
        )

    def test_ready_result_opens_local_catalog_without_online_open(self):
        result = LocalReadProbeSnapshot(
            "123",
            LocalReadProbeState.READY,
            (ChapterSnapshot("301", 1, "第 1 章", True),),
        )
        with patch.object(
            self.window,
            "_start_reader_session",
        ) as start, patch.object(self.window, "_open_reader") as online:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.SEARCH,
            )
            self.window._on_local_read_probe_completed(
                41,
                "probe_local_read",
                "123",
                result,
            )

        online.assert_not_called()
        start.assert_called_once()
        opened = start.call_args.args[0]
        self.assertEqual(opened.chapter_catalog.chapters, result.chapters)
        self.assertEqual(
            start.call_args.kwargs["content_mode"],
            ReaderContentMode.LOCAL,
        )
        self.assertEqual(
            start.call_args.kwargs["source"],
            ReaderSource.SEARCH,
        )

    def test_second_click_while_same_probe_is_active_is_ignored(self):
        self.window._request_local_first_reader(
            self.snapshot,
            ReaderSource.SEARCH,
        )
        self.window._request_local_first_reader(
            self.snapshot,
            ReaderSource.SEARCH,
        )

        self.assertEqual(self.controller.calls, ["123"])


if __name__ == "__main__":
    unittest.main()
