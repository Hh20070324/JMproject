import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterImageStatus,
    ChapterPackageStatus,
    ChapterSnapshot,
    LibraryChapterSnapshot,
    LibraryItem,
    LibraryLayout,
    ReaderContentMode,
    ReaderHistoryEntry,
    ReaderSource,
    SearchResultSnapshot,
)
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.pages.library_page import LibraryPage
from jm_downloader.qt.theme import ThemeManager


class UiReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError("prepared local catalog must be reused")

    async def load_chapter(self, catalog, photo_id):
        value = next(
            chapter for chapter in catalog.chapters if chapter.photo_id == photo_id
        )
        from jm_downloader.models import (
            ReaderChapterSnapshot,
            ReaderPageSnapshot,
            ReaderPageState,
        )

        return (
            ReaderChapterSnapshot(photo_id, value.index, value.title, 1),
            (ReaderPageSnapshot(photo_id, 1, 1, ReaderPageState.PLACEHOLDER),),
        )

    async def fetch_page(self, *_args, **_kwargs):
        raise RuntimeError("page pixels are outside this UI contract")

    async def close(self):
        return True


class FakeLibraryController(QObject):
    items_reset = Signal(object)
    loading_changed = Signal(bool)
    busy_albums_changed = Signal(object)
    active_albums_changed = Signal(object)
    command_failed = Signal(str, str, str)
    request_completed = Signal(int, str, str, object)
    request_failed = Signal(int, str, str, str)
    batch_delete_finished = Signal(str, object, object)

    def __init__(self, items):
        super().__init__()
        self.items = list(items)
        self.requests = []

    def list_items(self):
        return list(self.items)

    def active_album_ids(self):
        return frozenset()

    def busy_album_ids(self):
        return frozenset()

    def has_pending_mutations(self):
        return False

    def check_chapters(self, album_id):
        request_id = len(self.requests) + 1
        self.requests.append((request_id, album_id))
        return request_id

    def refresh(self):
        pass

    def open_item(self, *_args):
        pass

    def delete_item(self, *_args):
        pass

    def batch_delete(self, *_args):
        return True


def library_item(album_id, layout, *, images):
    return LibraryItem(
        album_id=album_id,
        title=f"漫画 {album_id}",
        layout=layout,
        chapter_count=1,
        image_count=1 if images else 0,
        image_size=10 if images else 0,
        preview_path=None,
        pdf_directory=None,
        pdf_size=0,
    )


def chapter_snapshot(photo_id, status):
    return LibraryChapterSnapshot(
        album_id="123",
        photo_id=photo_id,
        index=int(photo_id) - 300,
        title=f"章节 {photo_id}",
        image_directory=Path("chapter") if status is ChapterImageStatus.COMPLETE else None,
        package_path=None,
        page_count=3,
        valid_image_count=3 if status is ChapterImageStatus.COMPLETE else 0,
        image_status=status,
        package_format="images",
        package_status=ChapterPackageStatus.NOT_APPLICABLE,
        downloaded_at_utc=None,
        can_rebuild=False,
        can_redownload=status is not ChapterImageStatus.COMPLETE,
        can_delete_images=status is ChapterImageStatus.COMPLETE,
        can_delete_package=False,
        can_delete_all=True,
    )


class LocalLibraryEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = FakeLibraryController(
            (
                library_item("123", LibraryLayout.MANAGED, images=True),
                library_item("456", LibraryLayout.LEGACY, images=True),
                library_item("789", LibraryLayout.MANAGED, images=False),
            )
        )
        self.page = LibraryPage(self.controller)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    def test_read_button_only_appears_for_managed_items_with_images(self):
        self.assertTrue(self.page.item_card("123").read_button.isVisible())
        self.assertFalse(self.page.item_card("456").read_button.isVisible())
        self.assertFalse(self.page.item_card("789").read_button.isVisible())

    def test_click_checks_offline_then_emits_only_complete_chapters(self):
        ready = []
        self.page.local_read_ready.connect(
            lambda snapshot, history: ready.append((snapshot, history))
        )

        self.page.item_card("123").read_button.click()
        self.assertEqual(self.controller.requests, [(1, "123")])
        self.assertEqual(ready, [])
        self.controller.request_completed.emit(
            1,
            "check_chapters",
            "123",
            (
                chapter_snapshot("301", ChapterImageStatus.COMPLETE),
                chapter_snapshot("302", ChapterImageStatus.DAMAGED),
            ),
        )

        self.assertEqual(len(ready), 1)
        snapshot, history = ready[0]
        self.assertIsNone(history)
        self.assertEqual(
            [value.photo_id for value in snapshot.chapter_catalog.chapters],
            ["301"],
        )
        self.assertTrue(snapshot.chapter_catalog.chapters[0].downloaded)

    def test_no_complete_chapter_reports_once_without_opening_reader(self):
        failures = []
        self.page.local_read_failed.connect(
            lambda history, message: failures.append((history, message))
        )
        self.page.prepare_local_read("123")
        self.controller.request_completed.emit(
            1,
            "check_chapters",
            "123",
            (chapter_snapshot("301", ChapterImageStatus.MISSING),),
        )

        self.assertEqual(len(failures), 1)
        self.assertIn("没有图片完整", failures[0][1])


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


class LocalHistoryFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(
            ThemeManager("light"),
            persist_window_state=False,
        )
        self.window.reader_window = object()
        self.entry = ReaderHistoryEntry(
            album_id="123",
            title="本地漫画",
            photo_id="301",
            chapter_title="第 1 章",
            chapter_index=1,
            page_number=2,
            page_count=5,
            read_at_utc="2026-07-29T00:00:00Z",
            source=ReaderSource.LOCAL_LIBRARY,
            content_mode=ReaderContentMode.LOCAL,
        )

    def tearDown(self):
        self.window.reader_window = None
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_local_history_requests_offline_check_before_any_online_open(self):
        calls = []
        page = self.window.page("library")
        with patch.object(
            page,
            "prepare_local_read",
            side_effect=lambda album_id, entry: calls.append((album_id, entry)),
        ), patch.object(self.window, "_start_reader_session") as start:
            self.window._open_reader_history(self.entry)

        self.assertEqual(calls, [("123", self.entry)])
        start.assert_not_called()

    def test_missing_local_history_defaults_and_escape_to_cancel(self):
        with patch(
            "jm_downloader.qt.main_window.QMessageBox",
            FakeMessageBox,
        ), patch.object(self.window, "_start_reader_session") as start:
            FakeMessageBox.selection = "取消"
            self.window._offer_online_fallback(self.entry, "本地章节不可用")
            dialog = FakeMessageBox.instance
            self.assertIs(dialog.default, dialog.buttons["取消"])
            self.assertIs(dialog.escape, dialog.buttons["取消"])
            start.assert_not_called()

            FakeMessageBox.selection = "转为在线阅读"
            self.window._offer_online_fallback(self.entry, "本地章节不可用")
            self.assertEqual(start.call_count, 1)
            self.assertEqual(
                start.call_args.kwargs["content_mode"],
                ReaderContentMode.ONLINE,
            )

    def test_missing_history_chapter_does_not_open_other_local_chapter(self):
        snapshot = SearchResultSnapshot(
            "123",
            "本地漫画",
            chapter_catalog=ChapterCatalogSnapshot(
                "123",
                "本地漫画",
                (ChapterSnapshot("302", 2, "第 2 章", True),),
            ),
        )
        with patch.object(
            self.window,
            "_offer_online_fallback",
        ) as fallback, patch.object(
            self.window,
            "_start_reader_session",
        ) as start:
            self.window._open_local_reader_ready(snapshot, self.entry)

        fallback.assert_called_once()
        start.assert_not_called()


class LocalReaderWindowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.online = UiReaderService()
        self.local = UiReaderService()
        self.reader_controller = ReaderController(
            self.online,
            local_service=self.local,
            result_interval_ms=5,
        )
        self.window = MainWindow(
            ThemeManager("light"),
            reader_controller=self.reader_controller,
            persist_window_state=False,
        )
        self.snapshot = SearchResultSnapshot(
            "123",
            "本地漫画",
            chapter_catalog=ChapterCatalogSnapshot(
                "123",
                "本地漫画",
                (ChapterSnapshot("301", 1, "第 1 章", True),),
            ),
        )

    def tearDown(self):
        self.reader_controller.shutdown(2.0)
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_prepared_local_catalog_uses_one_window_and_downloaded_state(self):
        self.window._open_local_reader_ready(self.snapshot, None)

        self.assertTrue(
            self.wait_until(
                lambda: self.window.page("reader").current_photo_id == "301"
            )
        )
        self.assertEqual(
            self.window.reader_window.session_content_mode,
            ReaderContentMode.LOCAL,
        )
        self.assertEqual(
            self.window.page("reader").download_state.value,
            "downloaded",
        )

    def test_same_album_different_mode_requires_reuse_confirmation(self):
        self.window._open_local_reader_ready(self.snapshot, None)
        self.assertTrue(self.window.reader_window.has_session)
        with patch.object(
            self.window,
            "_confirm_reader_reuse",
            return_value=False,
        ) as confirm:
            self.window._open_reader(self.snapshot, ReaderSource.SEARCH)

        confirm.assert_called_once_with(
            "本地漫画",
            ReaderContentMode.ONLINE,
        )
        self.assertEqual(
            self.window.reader_window.session_content_mode,
            ReaderContentMode.LOCAL,
        )

if __name__ == "__main__":
    unittest.main()
