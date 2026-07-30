import os
import unittest
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    LocalReadProbeSnapshot,
    LocalReadProbeState,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager


class _SequencedProbeController:
    def __init__(self):
        self.calls = []

    def probe_local_read(self, album_id):
        self.calls.append(str(album_id))
        return len(self.calls)

    def has_pending_mutations(self):
        return False


class V322ReadRouteReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-route-reliability-tests"]
        )

    def setUp(self):
        self.window = MainWindow(
            ThemeManager("light"),
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.controller = _SequencedProbeController()
        self.window.library_controller = self.controller

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    @staticmethod
    def snapshot(album_id):
        return SearchResultSnapshot(str(album_id), f"漫画 {album_id}")

    def test_cross_page_late_result_cannot_open_or_prompt_for_old_album(self):
        first = self.snapshot("101")
        second = self.snapshot("202")
        with patch.object(
            self.window,
            "_open_reader",
        ) as online, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._request_local_first_reader(
                first,
                ReaderSource.SEARCH,
            )
            self.window._request_local_first_reader(
                second,
                ReaderSource.FAVORITES,
            )
            self.window._on_local_read_probe_completed(
                1,
                "probe_local_read",
                "101",
                LocalReadProbeSnapshot(
                    "101",
                    LocalReadProbeState.UNAVAILABLE,
                ),
            )
            online.assert_not_called()
            fallback.assert_not_called()

            self.window._on_local_read_probe_completed(
                2,
                "probe_local_read",
                "202",
                LocalReadProbeSnapshot(
                    "202",
                    LocalReadProbeState.ABSENT,
                ),
            )

        online.assert_called_once_with(
            second,
            ReaderSource.FAVORITES,
        )
        fallback.assert_not_called()

    def test_invalid_current_result_restores_button_and_uses_safe_fallback(self):
        snapshot = self.snapshot("303")
        busy = []
        page = self.window.page("downloads")
        with patch.object(
            page,
            "set_read_probe_busy",
            side_effect=lambda album_id, value: busy.append(
                (album_id, value)
            ),
        ), patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._request_local_first_reader(
                snapshot,
                ReaderSource.SEARCH,
            )
            self.window._on_local_read_probe_completed(
                1,
                "wrong_command",
                "outside",
                object(),
            )

        self.assertEqual(busy, [("303", True), ("303", False)])
        self.assertIsNone(self.window._active_local_read_route)
        self.assertEqual(
            fallback.call_args.args[2],
            "本地章节检查结果无效。",
        )

    def test_dispose_invalidates_inflight_result_without_opening_or_dialog(self):
        snapshot = self.snapshot("404")
        self.window._request_local_first_reader(
            snapshot,
            ReaderSource.SEARCH,
        )
        with patch.object(
            self.window,
            "_open_reader",
        ) as online, patch.object(
            self.window,
            "_start_reader_session",
        ) as local, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._dispose_search()
            self.window._on_local_read_probe_completed(
                1,
                "probe_local_read",
                "404",
                LocalReadProbeSnapshot(
                    "404",
                    LocalReadProbeState.ABSENT,
                ),
            )

        online.assert_not_called()
        local.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(self.window._local_read_route_requests, {})

    def test_many_superseded_requests_only_allow_latest_result_to_act(self):
        snapshots = [self.snapshot(index) for index in range(1, 31)]
        with patch.object(
            self.window,
            "_open_reader",
        ) as online, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            for snapshot in snapshots:
                self.window._request_local_first_reader(
                    snapshot,
                    ReaderSource.SEARCH,
                )
            for request_id, snapshot in enumerate(snapshots[:-1], 1):
                self.window._on_local_read_probe_completed(
                    request_id,
                    "probe_local_read",
                    snapshot.album_id,
                    LocalReadProbeSnapshot(
                        snapshot.album_id,
                        LocalReadProbeState.UNAVAILABLE,
                    ),
                )
            online.assert_not_called()
            fallback.assert_not_called()

            latest = snapshots[-1]
            self.window._on_local_read_probe_completed(
                len(snapshots),
                "probe_local_read",
                latest.album_id,
                LocalReadProbeSnapshot(
                    latest.album_id,
                    LocalReadProbeState.ABSENT,
                ),
            )

        online.assert_called_once_with(latest, ReaderSource.SEARCH)
        fallback.assert_not_called()
        self.assertEqual(self.window._local_read_route_requests, {})


if __name__ == "__main__":
    unittest.main()
