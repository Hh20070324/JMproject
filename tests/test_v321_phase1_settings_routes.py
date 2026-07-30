import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QMenu, QToolButton

from jm_downloader.models import (
    API_ROUTES,
    LEGACY_API_ROUTES,
    TaskConfig,
)
from jm_downloader.option_config import API_ROUTE_LABELS, QueryEngineState
from jm_downloader.qt.pages.settings_page import SettingsPage
from jm_downloader.settings import AppSettings, SettingsValidationError
from jm_downloader.task_store import StoredTask, TaskStoreValidationError


NEW_ROUTES = (
    "www.cdnhjk.net",
    "www.cdngwc.cc",
    "www.cdngwc.net",
    "www.cdngwc.club",
)


class V321SettingsAndRouteTests(unittest.TestCase):
    def test_query_engine_defaults_round_trips_and_is_independent(self):
        settings = AppSettings(download_engine="sync", query_engine="async")
        restored = AppSettings.from_dict(settings.to_dict())

        self.assertEqual(restored.download_engine, "sync")
        self.assertEqual(restored.query_engine, "async")
        self.assertEqual(restored.task_config().download_engine, "sync")
        state = QueryEngineState(restored.query_engine)
        state.set("sync")
        self.assertEqual(state.get(), "sync")
        with self.assertRaises(ValueError):
            state.set("invalid")

    def test_old_settings_without_query_field_defaults_to_async(self):
        payload = AppSettings().to_dict()
        del payload["query"]

        self.assertEqual(AppSettings.from_dict(payload).query_engine, "async")

    def test_each_known_old_settings_route_migrates_to_auto(self):
        for old_route in LEGACY_API_ROUTES:
            with self.subTest(route=old_route):
                payload = AppSettings().to_dict()
                payload["download"]["api_route"] = old_route
                restored = AppSettings.from_dict(payload)
                self.assertEqual(restored.api_route, "auto")
                self.assertEqual(
                    restored.to_dict()["download"]["api_route"],
                    "auto",
                )

    def test_each_known_old_task_route_migrates_to_auto(self):
        base = {
            "id": "task-1",
            "album_id": "123",
            "title": None,
            "status": "paused",
            "progress": 1,
            "chapter": "",
            "page": "",
            "error": None,
            "selected_chapter_ids": ["301"],
            "force_redownload_chapter_ids": [],
            "paths": {"pictures": "Pictures", "pdfs": "PDFs"},
        }
        for old_route in LEGACY_API_ROUTES:
            with self.subTest(route=old_route):
                payload = dict(base)
                payload["download"] = {
                    "engine": "async",
                    "api_route": old_route,
                    "package_format": "pdf",
                    "image_format": "jpg",
                    "image_concurrency": 16,
                    "multi_chapter_download_behavior": "parallel",
                }
                restored = StoredTask.from_dict(payload)
                self.assertEqual(restored.config.api_route, "auto")
                self.assertEqual(
                    restored.to_dict()["download"]["api_route"],
                    "auto",
                )

    def test_unknown_routes_and_query_engines_remain_invalid(self):
        payload = AppSettings().to_dict()
        payload["download"]["api_route"] = "unknown.invalid"
        with self.assertRaises(SettingsValidationError):
            AppSettings.from_dict(payload)

        payload = AppSettings().to_dict()
        payload["query"]["engine"] = "future"
        with self.assertRaises(SettingsValidationError):
            AppSettings.from_dict(payload)

        task = TaskConfig(api_route="unknown.invalid")
        with self.assertRaises(ValueError):
            task.validate()

    def test_new_routes_are_the_only_visible_fixed_choices(self):
        self.assertEqual(API_ROUTES, frozenset(("auto", *NEW_ROUTES)))
        self.assertEqual(tuple(API_ROUTE_LABELS), ("auto", *NEW_ROUTES))
        for old_route in LEGACY_API_ROUTES:
            self.assertNotIn(old_route, API_ROUTE_LABELS)


class V321SettingsChoiceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v321-settings-choice-tests"]
        )

    def test_every_choice_uses_a_themed_instant_popup_toolbutton(self):
        page = SettingsPage()
        page.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        try:
            self.assertEqual(page.findChildren(QComboBox), [])
            pairs = (
                (page.download_engine_button, page.download_engine_menu),
                (page.query_engine_button, page.query_engine_menu),
                (page.api_route_button, page.api_route_menu),
                (page.package_format_button, page.package_format_menu),
                (page.image_format_button, page.image_format_menu),
                (
                    page.multi_chapter_behavior_button,
                    page.multi_chapter_behavior_menu,
                ),
                (page.log_level_button, page.log_level_menu),
                (page.startup_page_button, page.startup_page_menu),
                (page.reader_layout_button, page.reader_layout_menu),
                (page.reader_zoom_button, page.reader_zoom_menu),
            )
            for button, menu in pairs:
                self.assertIsInstance(button, QToolButton)
                self.assertIsInstance(menu, QMenu)
                self.assertIs(button.menu(), menu)
                self.assertEqual(
                    button.popupMode(),
                    QToolButton.ToolButtonPopupMode.InstantPopup,
                )
                self.assertTrue(button.property("settingsChoice"))
                self.assertTrue(menu.property("settingsChoice"))
        finally:
            page.close()
            page.deleteLater()
            self.app.processEvents()

    def test_query_choice_changes_once_and_keeps_download_choice(self):
        page = SettingsPage()
        try:
            page._set_download_engine("sync")
            page._query_engine_actions["sync"].trigger()
            self.app.processEvents()
            self.assertEqual(page._selected_download_engine(), "sync")
            self.assertEqual(page._selected_query_engine(), "sync")
            self.assertIn("同步线程", page.query_engine_button.text())
        finally:
            page.close()
            page.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
