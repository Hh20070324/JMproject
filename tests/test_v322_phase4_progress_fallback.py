import os
import unittest
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import (
    ChapterSnapshot,
    LocalReadProbeSnapshot,
    LocalReadProbeState,
    ReaderContentMode,
    ReaderHistoryEntry,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager


def history(photo_id="301", page=3):
    return ReaderHistoryEntry(
        album_id="123",
        title="测试漫画",
        photo_id=photo_id,
        chapter_title="历史章节",
        chapter_index=1,
        page_number=page,
        page_count=8,
        read_at_utc="2026-07-31T00:00:00Z",
        source=ReaderSource.SEARCH,
        content_mode=ReaderContentMode.LOCAL,
    )


class _HistoryStore:
    def __init__(self, entry):
        self.entry = entry

    def find(self, _album_id):
        return self.entry


class _ProbeController:
    def probe_local_read(self, _album_id):
        return 91

    def has_pending_mutations(self):
        return False


class _FakeMessageBox:
    class Icon:
        Warning = object()

    class ButtonRole:
        AcceptRole = object()
        RejectRole = object()

    selection = "取消"
    instance = None

    def __init__(self, _parent=None):
        self.__class__.instance = self
        self.buttons = {}
        self.default = None
        self.escape = None

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


class V322ProgressAndFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-progress-fallback-tests"]
        )

    def setUp(self):
        self.window = MainWindow(
            ThemeManager("light"),
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.window.library_controller = _ProbeController()
        self.snapshot = SearchResultSnapshot("123", "测试漫画")
        self.ready = LocalReadProbeSnapshot(
            "123",
            LocalReadProbeState.READY,
            (
                ChapterSnapshot("301", 1, "第 1 章", True),
                ChapterSnapshot("302", 2, "第 2 章", True),
            ),
        )

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _complete_ready(self):
        self.window._on_local_read_probe_completed(
            91,
            "probe_local_read",
            "123",
            self.ready,
        )

    def test_valid_local_history_resumes_chapter_and_page(self):
        self.window.reader_history_store = _HistoryStore(
            history("302", 5)
        )
        with patch.object(
            self.window,
            "_start_reader_session",
        ) as start:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.FAVORITES,
            )
            self._complete_ready()

        self.assertEqual(
            start.call_args.kwargs["preferred_photo_id"],
            "302",
        )
        self.assertEqual(
            start.call_args.kwargs["preferred_page"],
            5,
        )
        self.assertIsNone(start.call_args.kwargs["notice"])
        self.assertEqual(
            start.call_args.kwargs["source"],
            ReaderSource.FAVORITES,
        )

    def test_missing_history_chapter_uses_first_local_chapter_with_notice(self):
        self.window.reader_history_store = _HistoryStore(
            history("999", 7)
        )
        with patch.object(
            self.window,
            "_start_reader_session",
        ) as start, patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.SEARCH,
            )
            self._complete_ready()

        fallback.assert_not_called()
        self.assertIsNone(
            start.call_args.kwargs["preferred_photo_id"]
        )
        self.assertEqual(start.call_args.kwargs["preferred_page"], 1)
        self.assertIn(
            "已从首个完整章节开始",
            start.call_args.kwargs["notice"],
        )

    def test_unavailable_local_content_defaults_and_escape_to_cancel(self):
        with patch(
            "jm_downloader.qt.main_window.QMessageBox",
            _FakeMessageBox,
        ), patch.object(self.window, "_open_reader") as online:
            _FakeMessageBox.selection = "取消"
            self.window._offer_snapshot_online_fallback(
                self.snapshot,
                ReaderSource.SEARCH,
                "本地没有图片完整的章节。",
            )

            dialog = _FakeMessageBox.instance
            self.assertIs(dialog.default, dialog.buttons["取消"])
            self.assertIs(dialog.escape, dialog.buttons["取消"])
            online.assert_not_called()

            _FakeMessageBox.selection = "转为在线阅读"
            self.window._offer_snapshot_online_fallback(
                self.snapshot,
                ReaderSource.SEARCH,
                "本地没有图片完整的章节。",
            )

        online.assert_called_once_with(
            self.snapshot,
            ReaderSource.SEARCH,
        )

    def test_probe_failure_uses_safe_message_and_never_directly_opens_online(self):
        with patch.object(
            self.window,
            "_offer_snapshot_online_fallback",
        ) as fallback, patch.object(
            self.window,
            "_open_reader",
        ) as online:
            self.window._request_local_first_reader(
                self.snapshot,
                ReaderSource.SEARCH,
            )
            self.window._on_local_read_probe_failed(
                91,
                "probe_local_read",
                "123",
                r"C:\Users\private\Pictures access failed",
            )

        online.assert_not_called()
        self.assertEqual(
            fallback.call_args.args[2],
            "本地章节检查失败，请稍后重试。",
        )
        self.assertNotIn("C:\\", fallback.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
