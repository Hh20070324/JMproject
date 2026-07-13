import os
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.desktop_runtime import WINDOW_TITLE
from jm_downloader.qt.app import load_stylesheet, resource_path
from jm_downloader.qt.main_window import MainWindow


class QtMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["qt-tests"])

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

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
        self.assertEqual(page_ids, ["downloadPage", "libraryPage", "settingsPage"])

        self.window.navigation_button("library").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "library")

        self.window.navigation_button("settings").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "settings")

        self.window.navigation_button("downloads").click()
        self.app.processEvents()
        self.assertEqual(self.window.current_page, "downloads")

    def test_rejects_unknown_page(self):
        with self.assertRaises(ValueError):
            self.window.select_page("missing")

    def test_stylesheet_resource_is_available(self):
        self.assertTrue(resource_path("styles.qss").is_file())
        stylesheet = load_stylesheet()
        self.assertIn("QToolButton#navButton", stylesheet)
        self.assertIn("QLabel#pageTitle", stylesheet)


if __name__ == "__main__":
    unittest.main()
