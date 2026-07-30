import os
import unittest
from types import SimpleNamespace
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
from jm_downloader.qt.pages.favorites_page import FavoritesPage
from jm_downloader.qt.theme import ThemeManager


class _ProbeController:
    def __init__(self):
        self.calls = []

    def probe_local_read(self, album_id):
        self.calls.append(album_id)
        return 72

    def has_pending_mutations(self):
        return False


class V322FavoritesBusyStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-favorites-route-tests"]
        )

    def setUp(self):
        self.page = FavoritesPage(reader_available=True)
        self.item = SimpleNamespace(
            album_id="123",
            title="收藏漫画",
            authors=(),
            tags=(),
        )

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def test_busy_state_survives_card_rebuild_and_restores_new_card(self):
        self.page.set_read_probe_busy("123", True)
        self.page._set_cards((self.item,))

        first = self.page.favorite_cards[0]
        self.assertEqual(first.read_button.text(), "检查中…")
        self.assertFalse(first.read_button.isEnabled())

        self.page._set_cards((self.item,))
        rebuilt = self.page.favorite_cards[0]
        self.assertIsNot(rebuilt, first)
        self.assertEqual(rebuilt.read_button.text(), "检查中…")

        self.page.set_read_probe_busy("123", False)
        self.assertEqual(rebuilt.read_button.text(), "阅读")
        self.assertTrue(rebuilt.read_button.isEnabled())

    def test_favorite_card_keeps_favorites_source(self):
        events = []
        self.page.read_requested.connect(
            lambda snapshot, source: events.append((snapshot, source))
        )
        self.page._set_cards((self.item,))

        self.page.favorite_cards[0].read_button.click()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0].album_id, "123")
        self.assertEqual(events[0][1], ReaderSource.FAVORITES)


class V322FavoritesRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-favorites-main-window-tests"]
        )

    def setUp(self):
        self.window = MainWindow(
            ThemeManager("light"),
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.controller = _ProbeController()
        self.window.library_controller = self.controller
        self.snapshot = SearchResultSnapshot("123", "收藏漫画")

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_favorite_ready_result_opens_local_and_preserves_source(self):
        result = LocalReadProbeSnapshot(
            "123",
            LocalReadProbeState.READY,
            (ChapterSnapshot("301", 1, "第 1 章", True),),
        )
        busy = []
        page = self.window.page("favorites")
        with patch.object(
            page,
            "set_read_probe_busy",
            side_effect=lambda album_id, value: busy.append(
                (album_id, value)
            ),
        ), patch.object(
            self.window,
            "_start_reader_session",
        ) as start, patch.object(self.window, "_open_reader") as online:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.FAVORITES,
            )
            self.window._on_local_read_probe_completed(
                72,
                "probe_local_read",
                "123",
                result,
            )

        self.assertEqual(self.controller.calls, ["123"])
        self.assertEqual(busy, [("123", True), ("123", False)])
        online.assert_not_called()
        self.assertEqual(
            start.call_args.kwargs["source"],
            ReaderSource.FAVORITES,
        )
        self.assertEqual(
            start.call_args.kwargs["content_mode"],
            ReaderContentMode.LOCAL,
        )

    def test_favorite_absence_uses_online_path_without_prompt(self):
        with patch.object(
            self.window,
            "_open_reader",
        ) as online, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.FAVORITES,
            )
            self.window._on_local_read_probe_completed(
                72,
                "probe_local_read",
                "123",
                LocalReadProbeSnapshot(
                    "123",
                    LocalReadProbeState.ABSENT,
                ),
            )

        fallback.assert_not_called()
        online.assert_called_once_with(
            self.snapshot,
            ReaderSource.FAVORITES,
        )


if __name__ == "__main__":
    unittest.main()
