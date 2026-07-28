from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QToolButton

from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.pages.settings_page import SettingsPage
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.settings import (
    READER_ZOOM_LEVELS,
    AppPaths,
    AppSettings,
    SettingsValidationError,
)


class V31ReaderSettingsModelTests(unittest.TestCase):
    def test_v30_payload_uses_reader_defaults_without_losing_other_values(self):
        payload = AppSettings(
            pictures_directory="Artwork",
            pdf_directory="Packages",
            theme="dark",
            reader_layout="fit_page",
        ).to_dict()
        payload["appearance"].pop("reader_zoom_percent")
        for key in ("reader_width", "reader_height", "reader_x", "reader_y"):
            payload["window"].pop(key)

        restored = AppSettings.from_dict(payload)

        self.assertEqual(restored.pictures_directory, "Artwork")
        self.assertEqual(restored.pdf_directory, "Packages")
        self.assertEqual(restored.theme, "dark")
        self.assertEqual(restored.reader_layout, "fit_page")
        self.assertEqual(restored.reader_zoom_percent, 100)
        self.assertEqual(
            (restored.reader_window_width, restored.reader_window_height),
            (1000, 760),
        )
        self.assertIsNone(restored.reader_window_x)
        self.assertIsNone(restored.reader_window_y)

    def test_reader_zoom_and_geometry_round_trip_in_schema_v1(self):
        expected = replace(
            AppSettings(),
            reader_layout="fit_page",
            reader_zoom_percent=150,
            reader_window_width=1320,
            reader_window_height=880,
            reader_window_x=-720,
            reader_window_y=120,
        )

        restored = AppSettings.from_dict(expected.to_dict())

        self.assertEqual(restored, expected)
        self.assertEqual(restored.schema_version, 1)

    def test_reader_values_are_strictly_validated(self):
        invalid = (
            replace(AppSettings(), reader_zoom_percent=True),
            replace(AppSettings(), reader_zoom_percent=101),
            replace(AppSettings(), reader_zoom_percent=100.0),
            replace(AppSettings(), reader_window_width=759),
            replace(AppSettings(), reader_window_height=10001),
            replace(AppSettings(), reader_window_x=True),
            replace(AppSettings(), reader_window_y=100001),
        )
        for settings in invalid:
            with self.subTest(settings=settings):
                with self.assertRaises(SettingsValidationError):
                    settings.validate()

    def test_old_settings_file_loads_without_recovery_or_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths(Path(temporary))
            payload = AppSettings().to_dict()
            payload["appearance"].pop("reader_zoom_percent")
            for key in (
                "reader_width",
                "reader_height",
                "reader_x",
                "reader_y",
            ):
                payload["window"].pop(key)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            paths.settings_file.write_bytes(raw)
            store = SettingsStore(paths)

            loaded = store.load()

            self.assertEqual(loaded.reader_zoom_percent, 100)
            self.assertEqual(paths.settings_file.read_bytes(), raw)
            self.assertIsNone(store.last_recovery_backup)


class V31ReaderSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-settings-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.controller = SettingsController(SettingsStore(self.paths))
        self.page = SettingsPage(settings_controller=self.controller)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.resize(760, 720)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_reader_selectors_are_exclusive_instant_popup_toolbuttons(self):
        for button, actions in (
            (
                self.page.reader_layout_button,
                self.page.reader_layout_menu.actions(),
            ),
            (
                self.page.reader_zoom_button,
                self.page.reader_zoom_menu.actions(),
            ),
        ):
            self.assertIsInstance(button, QToolButton)
            self.assertEqual(
                button.popupMode(),
                QToolButton.ToolButtonPopupMode.InstantPopup,
            )
            self.assertTrue(all(action.isCheckable() for action in actions))

        self.assertEqual(
            [action.data() for action in self.page.reader_layout_menu.actions()],
            ["fit_width", "fit_page"],
        )
        self.assertEqual(
            [action.data() for action in self.page.reader_zoom_menu.actions()],
            sorted(READER_ZOOM_LEVELS),
        )

    def test_reader_choices_save_and_follow_external_settings_changes(self):
        self.page.reader_layout_menu.actions()[1].trigger()
        self.page.reader_zoom_menu.actions()[-1].trigger()
        self.page.save_button.click()

        saved = self.controller.settings
        self.assertEqual(saved.reader_layout, "fit_page")
        self.assertEqual(saved.reader_zoom_percent, 150)
        self.assertEqual(SettingsStore(self.paths).load(), saved)

        changed = replace(
            saved,
            reader_layout="fit_width",
            reader_zoom_percent=50,
        )
        self.assertTrue(self.controller.save(changed))
        self.app.processEvents()

        self.assertEqual(
            self.page._selected_reader_layout(),
            "fit_width",
        )
        self.assertEqual(self.page._selected_reader_zoom(), 50)
        self.assertIn("适合宽度", self.page.reader_layout_button.text())
        self.assertIn("50%", self.page.reader_zoom_button.text())


if __name__ == "__main__":
    unittest.main()
