import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop, QTimer, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import (
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.search_controller import SearchController
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.pages.settings_page import SettingsPage
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.search import SearchUnavailable
from jm_downloader.search_history import (
    MAX_SEARCH_HISTORY_ENTRIES,
    SearchHistoryStore,
)
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    def protect(self, plaintext):
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("wrong user or corrupt data")
        return ciphertext.removeprefix(b"protected:")[::-1]


class SearchHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.protected = ProtectedStore.search_history(
            self.paths,
            DeterministicProtector(),
        )
        self.clock = 0

        def now():
            self.clock += 1
            return (
                datetime(2026, 7, 26, tzinfo=timezone.utc)
                + timedelta(seconds=self.clock)
            ).isoformat().replace("+00:00", "Z")

        self.store = SearchHistoryStore(self.protected, now=now)

    def tearDown(self):
        self.temporary.cleanup()

    def test_encrypted_round_trip_deduplicates_and_keeps_recent_fifty(self):
        self.store.record("keyword", "  Alpha   Story ")
        self.store.record("keyword", "alpha story")
        self.store.record("jm_id", "JM00123")
        initial = self.store.load()
        self.assertEqual(
            [
                (entry.kind, entry.text)
                for entry in initial
            ],
            [("jm_id", "123"), ("keyword", "alpha story")],
        )
        for value in range(60):
            self.store.record("keyword", f"query-{value}")

        entries = self.store.load()

        self.assertEqual(len(entries), MAX_SEARCH_HISTORY_ENTRIES)
        self.assertEqual(entries[0].text, "query-59")
        self.assertNotIn(
            b"query-59",
            self.paths.search_history_file.read_bytes(),
        )

    def test_single_remove_and_clear_update_the_encrypted_file(self):
        self.store.record("keyword", "alpha")
        self.store.record("jm_id", "123")

        remaining = self.store.remove("keyword", " ALPHA ")

        self.assertEqual(
            [(entry.kind, entry.text) for entry in remaining],
            [("jm_id", "123")],
        )
        self.store.clear()
        self.assertFalse(self.paths.search_history_file.exists())

    def test_unreadable_dpapi_payload_is_backed_up_and_degrades_empty(self):
        self.store.record("keyword", "alpha")
        self.paths.search_history_file.write_bytes(b"not-protected")

        restored = self.store.load()

        self.assertEqual(restored, ())
        self.assertFalse(self.paths.search_history_file.exists())
        self.assertIsNotNone(self.store.last_recovery_backup)
        self.assertEqual(
            self.store.last_recovery_backup.read_bytes(),
            b"not-protected",
        )


class ControlledSearchService:
    def __init__(self, behavior=None):
        self.behavior = behavior or (
            lambda request: SearchPageSnapshot(request, 0, 0, ())
        )
        self.calls = []

    def search(self, request):
        self.calls.append(request)
        return self.behavior(request)


class SearchHistoryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["search-history-controller-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.history = SearchHistoryStore(
            ProtectedStore.search_history(
                self.paths,
                DeterministicProtector(),
            )
        )
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.dispose()
            controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def make_controller(self, service):
        controller = SearchController(
            service,
            history_store=self.history,
            result_interval_ms=5,
        )
        self.controllers.append(controller)
        return controller

    def test_success_and_empty_result_record_but_failure_does_not(self):
        service = ControlledSearchService()
        controller = self.make_controller(service)
        controller.search(SearchMode.GENERAL, "  Alpha  ")
        self.assertTrue(
            self.wait_until(lambda: len(controller.history_entries()) == 1)
        )
        self.assertEqual(controller.history_entries()[0].text, "Alpha")

        service.behavior = lambda _request: (_ for _ in ()).throw(
            SearchUnavailable()
        )
        controller.search(SearchMode.GENERAL, "failure")
        self.process_for(80)
        self.assertEqual(len(controller.history_entries()), 1)

    def test_late_superseded_success_is_not_recorded(self):
        first_started = threading.Event()
        release_first = threading.Event()

        def behavior(request):
            if request.query == "first":
                first_started.set()
                release_first.wait(timeout=2)
            return SearchPageSnapshot(request, 0, 0, ())

        controller = self.make_controller(ControlledSearchService(behavior))
        controller.search(SearchMode.GENERAL, "first")
        self.assertTrue(first_started.wait(timeout=1))
        controller.search(SearchMode.GENERAL, "second")
        self.assertTrue(
            self.wait_until(
                lambda: any(
                    entry.text == "second"
                    for entry in controller.history_entries()
                )
            )
        )
        release_first.set()
        self.process_for(80)
        self.assertEqual(
            [entry.text for entry in controller.history_entries()],
            ["second"],
        )

    def test_menu_refills_and_submits_selected_history(self):
        self.history.record("keyword", "alpha")
        service = ControlledSearchService()
        controller = self.make_controller(service)
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            page._show_history(page.general_search_input)
            self.assertEqual(
                [entry.text for entry in page.keyword_history_menu.entries],
                ["alpha"],
            )

            page._use_history_entry(page.keyword_history_menu.entries[0])

            self.assertEqual(page.general_search_input.text(), "alpha")
            self.assertTrue(
                self.wait_until(lambda: bool(service.calls))
            )
            self.assertEqual(service.calls[0].query, "alpha")

            page._delete_history_entry(
                page.keyword_history_menu.entries[0]
            )
            self.assertEqual(controller.history_entries(), ())
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_input_click_schedules_history_popup_only_once(self):
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            page.general_search_button.setFocus()
            self.app.processEvents()
            with patch.object(page, "_show_history") as show_history:
                QTest.mouseClick(
                    page.general_search_input,
                    Qt.MouseButton.LeftButton,
                )
                self.process_for(20)

            show_history.assert_called_once_with(
                page.general_search_input
            )
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_popup_focus_return_does_not_reopen_history(self):
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            popup_focus = QFocusEvent(
                QEvent.Type.FocusIn,
                Qt.FocusReason.PopupFocusReason,
            )
            with patch.object(page, "_show_history") as show_history:
                self.app.sendEvent(
                    page.general_search_input,
                    popup_focus,
                )
                self.process_for(20)

            show_history.assert_not_called()
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_history_panel_never_takes_focus_and_typing_closes_it(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)

            self.assertTrue(page.keyword_history_menu.isVisible())
            self.assertFalse(page.keyword_history_menu.isWindow())
            self.assertEqual(
                page.keyword_history_menu.focusPolicy(),
                Qt.FocusPolicy.NoFocus,
            )

            QTest.keyClicks(page.general_search_input, "beta")
            self.process_for(20)

            self.assertEqual(page.general_search_input.text(), "beta")
            self.assertFalse(page.keyword_history_menu.isVisible())
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_second_input_click_closes_history_without_reopening(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertTrue(page.keyword_history_menu.isVisible())

            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(40)

            self.assertFalse(page.keyword_history_menu.isVisible())
            self.assertIsNone(page._history_popup_editor)
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_focus_out_and_outside_click_close_history_panel(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertTrue(page.keyword_history_menu.isVisible())

            focus_out = QFocusEvent(
                QEvent.Type.FocusOut,
                Qt.FocusReason.TabFocusReason,
            )
            self.app.sendEvent(
                page.general_search_input,
                focus_out,
            )
            self.process_for(20)
            self.assertFalse(page.keyword_history_menu.isVisible())

            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertTrue(page.keyword_history_menu.isVisible())

            QTest.mouseClick(
                page.general_search_button,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertFalse(page.keyword_history_menu.isVisible())
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_deleting_last_history_entry_closes_empty_panel(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertTrue(page.keyword_history_menu.isVisible())

            page._delete_history_entry(
                page.keyword_history_menu.entries[0]
            )
            self.process_for(20)

            self.assertEqual(controller.history_entries(), ())
            self.assertFalse(page.keyword_history_menu.isVisible())
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_page_hide_does_not_restore_history_panel_on_return(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = DownloadPage(search_controller=controller)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            QTest.mouseClick(
                page.general_search_input,
                Qt.MouseButton.LeftButton,
            )
            self.process_for(20)
            self.assertTrue(page.keyword_history_menu.isVisible())

            page.hide()
            self.process_for(20)
            page.show()
            self.process_for(20)

            self.assertFalse(page.keyword_history_menu.isVisible())
        finally:
            page.dispose()
            page.close()
            page.deleteLater()

    def test_settings_clear_uses_native_confirmation(self):
        self.history.record("keyword", "alpha")
        controller = self.make_controller(ControlledSearchService())
        page = SettingsPage(
            ThemeManager(),
            search_controller=controller,
        )
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.app.processEvents()
        try:
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page.clear_search_history_button.click()

            self.assertEqual(controller.history_entries(), ())
            self.assertFalse(self.paths.search_history_file.exists())
            self.assertEqual(
                page.search_history_status.text(),
                "已清除",
            )
        finally:
            page.close()
            page.deleteLater()

    def wait_until(self, predicate, timeout_ms=2000):
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        return predicate()

    def process_for(self, duration_ms):
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(duration_ms)
        loop.exec()


if __name__ == "__main__":
    unittest.main()
