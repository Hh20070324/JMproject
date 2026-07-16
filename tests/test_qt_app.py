import os
from pathlib import Path
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from jm_downloader.desktop_runtime import WINDOW_TITLE
from jm_downloader.models import (
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.qt.app import load_stylesheet, resource_path
from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.icons import svg_icon
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.qt.theme import Theme, ThemeManager
from jm_downloader.settings import AppPaths


class QtMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["qt-tests"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.settings_store = SettingsStore(self.paths)
        self.settings_controller = SettingsController(self.settings_store)
        self.theme_manager = ThemeManager(
            self.settings_controller.settings.theme
        )
        self.theme_manager.apply()
        self.window = MainWindow(
            self.theme_manager,
            settings_controller=self.settings_controller,
            persist_window_state=False,
        )
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_has_native_window_basics(self):
        self.assertEqual(self.window.windowTitle(), WINDOW_TITLE)
        self.assertGreaterEqual(self.window.minimumWidth(), 760)
        self.assertGreaterEqual(self.window.minimumHeight(), 520)
        self.assertFalse(self.window.windowIcon().isNull())

    def test_switches_between_persistent_pages(self):
        page_ids = [
            self.window.stack.widget(index).objectName()
            for index in range(self.window.stack.count())
        ]
        self.assertEqual(
            page_ids,
            ["downloadPage", "favoritesPage", "libraryPage", "settingsPage"],
        )

        self.window.navigation_button("favorites").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "favorites")

        self.window.navigation_button("library").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "library")

        self.window.navigation_button("settings").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "settings")

        self.window.navigation_button("downloads").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "downloads")

        download_page = self.window.page("downloads")
        download_page.general_search_input.setText("保留输入")
        self.window.select_page("settings")
        self.window.select_page("downloads")
        self.assertEqual(download_page.general_search_input.text(), "保留输入")

    def test_rejects_unknown_page(self):
        with self.assertRaises(ValueError):
            self.window.select_page("missing")

    def test_search_layout_and_real_result_cards(self):
        self.window.resize(760, 520)
        self.app.processEvents()
        self.app.processEvents()

        page = self.window.page("downloads")
        self.assertEqual(
            page.findChild(QLabel, "pageTitle").text(),
            "搜索与下载",
        )
        self.assertEqual(
            self.window.navigation_button("downloads").text(),
            "搜索与下载",
        )
        self.assertEqual(
            page.general_search_input.placeholderText(),
            "搜索漫画名、标签或作者",
        )
        self.assertIn("JM", page.jm_id_search_input.placeholderText())
        self.assertGreater(
            page.general_search_input.width(), page.jm_id_search_input.width()
        )
        self.assertFalse(
            page.general_search_input.geometry().intersects(
                page.jm_id_search_input.geometry()
            )
        )

        request = SearchRequest(SearchMode.GENERAL, "query")
        snapshot = SearchPageSnapshot(
            request,
            8,
            1,
            tuple(
                SearchResultSnapshot(
                    str(index),
                    f"真实标题 {index}",
                    ("作者",),
                    ("标签",),
                )
                for index in range(1, 9)
            ),
        )
        page._on_search_submitted(1, request)
        page._on_search_results(1, snapshot, False)
        self.app.processEvents()
        self.app.processEvents()

        self.assertEqual(len(page.comic_cards), 8)
        card = page.comic_cards[0]
        info = card.findChild(QWidget, "comicInfo")
        self.assertGreater(card.height(), card.width())
        self.assertLess(card.cover_label.geometry().top(), info.geometry().top())
        self.assertEqual(card.snapshot.title, "真实标题 1")
        self.assertEqual(page.column_count, 2)

        self.window.resize(1100, 720)
        self.app.processEvents()
        self.app.processEvents()
        self.assertEqual(page.column_count, 4)

    def test_switches_theme_from_settings_and_persists_it(self):
        settings_page = self.window.page("settings")
        light_stylesheet = self.app.styleSheet()
        self.assertTrue(settings_page.theme_button(Theme.LIGHT).isChecked())

        settings_page.theme_button(Theme.DARK).click()
        settings_page.save_button.click()
        self.app.processEvents()
        self.assertEqual(self.theme_manager.theme, Theme.DARK)
        self.assertTrue(settings_page.theme_button(Theme.DARK).isChecked())
        self.assertNotEqual(self.app.styleSheet(), light_stylesheet)

        restored = SettingsStore(self.paths).load()
        self.assertEqual(restored.theme, Theme.DARK.value)

    def test_unknown_saved_theme_falls_back_to_light(self):
        self.assertEqual(ThemeManager("missing").theme, Theme.LIGHT)

    def test_theme_stylesheet_resources_are_available(self):
        for theme in (Theme.LIGHT, Theme.DARK):
            path = resource_path(f"styles_{theme.value}.qss")
            self.assertTrue(path.is_file())
            stylesheet = load_stylesheet(theme)
            self.assertIn("QToolButton#navButton", stylesheet)
            self.assertIn("QFrame#comicCard", stylesheet)
            self.assertIn("QToolButton#searchModeButton", stylesheet)
            self.assertIn("QPushButton#searchResultActionButton", stylesheet)
            self.assertIn("QToolButton#themeButton", stylesheet)

    def test_selected_svg_icon_resources_render(self):
        for name in (
            "search",
            "folder",
            "bookmark",
            "settings",
            "arrow-left",
            "arrow-right",
            "plus",
            "minus",
            "download",
            "pause",
            "play",
            "stop",
            "user-check",
            "user-delete",
        ):
            with self.subTest(icon=name):
                path = resource_path(f"icons/{name}.svg")
                self.assertTrue(path.is_file())
                icon = svg_icon(name)
                self.assertFalse(icon.isNull())
                self.assertFalse(icon.pixmap(24, 24).isNull())


if __name__ == "__main__":
    unittest.main()
