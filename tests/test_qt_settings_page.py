import os
from pathlib import Path
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QSpinBox

from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.pages.settings_page import SettingsPage
from jm_downloader.qt.theme import Theme, ThemeManager
from jm_downloader.settings import AppPaths, AppSettings


class FakeSettingsStore:
    def __init__(self, root: Path, settings: AppSettings | None = None):
        self.paths = AppPaths(root=root)
        self.settings = settings or AppSettings()
        self.saved = []
        self.save_error = None

    def load(self):
        return self.settings

    def save(self, settings):
        if self.save_error is not None:
            raise self.save_error
        settings.validate()
        self.settings = settings
        self.saved.append(settings)

    def reset(self):
        defaults = AppSettings()
        self.save(defaults)
        return defaults


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["settings-page-tests"])

    def setUp(self):
        self.root = Path.cwd()
        initial = AppSettings(
            pictures_directory="Artwork",
            pdf_directory="Documents/PDF",
            max_concurrent_tasks=3,
            image_concurrency=24,
            log_level="WARNING",
            window_width=1280,
            window_height=800,
            startup_page="library",
            theme="light",
        )
        self.store = FakeSettingsStore(self.root, initial)
        self.controller = SettingsController(self.store)
        self.theme_manager = ThemeManager(Theme.LIGHT)
        self.page = SettingsPage(
            self.theme_manager,
            settings_controller=self.controller,
        )
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.resize(700, 520)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    def test_loads_every_supported_setting_into_native_controls(self):
        self.assertEqual(self.page.pictures_directory_input.text(), "Artwork")
        self.assertEqual(self.page.pdf_directory_input.text(), "Documents/PDF")
        self.assertEqual(self.page.max_concurrent_tasks_spin.value(), 3)
        self.assertEqual(self.page.image_concurrency_spin.value(), 24)
        self.assertEqual(self.page.log_level_combo.currentData(), "WARNING")
        self.assertEqual(self.page.startup_page_combo.currentData(), "library")
        self.assertEqual(self.page.window_width_spin.value(), 1280)
        self.assertEqual(self.page.window_height_spin.value(), 800)
        self.assertTrue(self.page.theme_button(Theme.LIGHT).isChecked())
        self.assertFalse(
            self.page.settings_scroll.horizontalScrollBar().isVisible()
        )
        downloads_index = self.page.startup_page_combo.findData("downloads")
        self.assertEqual(
            self.page.startup_page_combo.itemText(downloads_index),
            "搜索与下载",
        )

    def test_compact_steppers_have_separate_safe_click_targets(self):
        spin = self.page.max_concurrent_tasks_spin
        decrease = self.page.maxConcurrentTasks_decrease_button
        increase = self.page.maxConcurrentTasks_increase_button

        self.assertEqual(spin.buttonSymbols(), QSpinBox.ButtonSymbols.NoButtons)
        self.assertEqual(decrease.size().toTuple(), (28, 28))
        self.assertEqual(increase.size().toTuple(), (28, 28))
        self.assertFalse(decrease.geometry().intersects(spin.geometry()))
        self.assertFalse(increase.geometry().intersects(spin.geometry()))

        original = spin.value()
        increase.click()
        self.assertEqual(spin.value(), original + 1)
        decrease.click()
        self.assertEqual(spin.value(), original)

        spin.setValue(spin.minimum())
        self.assertFalse(decrease.isEnabled())
        self.assertTrue(increase.isEnabled())
        spin.setValue(spin.maximum())
        self.assertTrue(decrease.isEnabled())
        self.assertFalse(increase.isEnabled())

    def test_choice_combos_have_separate_safe_popup_targets(self):
        controls = (
            (self.page.log_level_combo, self.page.logLevel_popup_button),
            (self.page.startup_page_combo, self.page.startupPage_popup_button),
        )

        for combo, popup_button in controls:
            self.assertEqual(popup_button.size().toTuple(), (28, 28))
            self.assertFalse(
                popup_button.geometry().intersects(combo.geometry())
            )
            self.assertFalse(popup_button.icon().isNull())
            with patch.object(combo, "showPopup") as show_popup:
                popup_button.click()
                show_popup.assert_called_once_with()

    def test_theme_buttons_show_distinct_icons_and_text(self):
        light_button = self.page.theme_button(Theme.LIGHT)
        dark_button = self.page.theme_button(Theme.DARK)
        theme_control = light_button.parentWidget()
        for theme, label in (
            (Theme.LIGHT, "明亮"),
            (Theme.DARK, "黑暗"),
        ):
            button = self.page.theme_button(theme)
            self.assertEqual(button.text(), label)
            self.assertEqual(
                button.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
            )
            self.assertFalse(button.icon().isNull())
            self.assertEqual(button.size().toTuple(), (92, 36))
        self.assertEqual(theme_control.objectName(), "settingsThemeControl")
        self.assertFalse(
            light_button.geometry().intersects(dark_button.geometry())
        )
        self.assertEqual(
            dark_button.geometry().left()
            - light_button.geometry().right()
            - 1,
            8,
        )
        self.assertEqual(light_button.geometry().top(), dark_button.geometry().top())
        self.assertEqual(
            light_button.geometry().bottom(),
            dark_button.geometry().bottom(),
        )

    def test_theme_has_its_own_section_and_row_label(self):
        section_titles = [
            label.text()
            for label in self.page.findChildren(QLabel, "sectionTitle")
        ]
        self.assertEqual(
            section_titles,
            ["存储位置", "下载性能", "应用", "主题模式"],
        )

        theme_control = self.page.theme_button(Theme.LIGHT).parentWidget()
        theme_row = theme_control.parentWidget()
        row_label = theme_row.findChild(QLabel, "settingsLabel")
        self.assertIsNotNone(row_label)
        self.assertEqual(row_label.text(), "明暗切换")

    def test_window_size_fields_do_not_show_native_step_buttons(self):
        for spin in (self.page.window_width_spin, self.page.window_height_spin):
            self.assertEqual(
                spin.buttonSymbols(),
                QSpinBox.ButtonSymbols.NoButtons,
            )

    def test_saves_one_complete_snapshot_and_emits_change(self):
        changes = []
        self.controller.settings_changed.connect(changes.append)

        self.page.pictures_directory_input.setText("Images")
        self.page.pdf_directory_input.setText("Archive/PDF")
        self.page.max_concurrent_tasks_spin.setValue(4)
        self.page.image_concurrency_spin.setValue(32)
        self.page.log_level_combo.setCurrentIndex(
            self.page.log_level_combo.findData("DEBUG")
        )
        self.page.startup_page_combo.setCurrentIndex(
            self.page.startup_page_combo.findData("settings")
        )
        self.page.window_width_spin.setValue(1440)
        self.page.window_height_spin.setValue(900)
        self.page.theme_button(Theme.DARK).click()

        self.assertEqual(self.theme_manager.theme, Theme.LIGHT)
        self.page.save_button.click()

        saved = self.store.saved[-1]
        self.assertEqual(saved.pictures_directory, "Images")
        self.assertEqual(saved.pdf_directory, "Archive/PDF")
        self.assertEqual(saved.max_concurrent_tasks, 4)
        self.assertEqual(saved.image_concurrency, 32)
        self.assertEqual(saved.log_level, "DEBUG")
        self.assertEqual(saved.startup_page, "settings")
        self.assertEqual((saved.window_width, saved.window_height), (1440, 900))
        self.assertEqual(saved.theme, "dark")
        self.assertEqual(changes, [saved])
        self.assertEqual(
            self.page.save_status_label.text(),
            "设置已保存；部分设置重启后生效",
        )

    def test_typed_absolute_path_inside_root_is_saved_as_relative(self):
        self.page.pictures_directory_input.setText(
            str(self.root / "Images")
        )

        self.page.save_button.click()

        self.assertEqual(
            self.store.saved[-1].pictures_directory,
            "Images",
        )

    def test_save_status_and_actions_fit_compact_page(self):
        self.page.resize(488, 520)
        self.page.save_button.click()
        self.app.processEvents()

        action_bar = self.page.save_button.parentWidget()
        self.assertLessEqual(
            self.page.save_button.geometry().right(),
            action_bar.contentsRect().right(),
        )
        self.assertLess(
            self.page.save_status_label.geometry().right(),
            self.page.restore_defaults_button.geometry().left(),
        )

        self.page.max_concurrent_tasks_spin.setValue(5)
        self.assertEqual(self.page.save_status_label.text(), "")

    def test_directory_picker_keeps_paths_inside_portable_root_relative(self):
        selected = self.root / "New Pictures"
        with patch(
            "jm_downloader.qt.pages.settings_page.QFileDialog.getExistingDirectory",
            return_value=str(selected),
        ):
            self.page._choose_directory(self.page.pictures_directory_input)

        self.assertEqual(
            self.page.pictures_directory_input.text(),
            "New Pictures",
        )

    def test_restore_defaults_is_confirmed_and_saved(self):
        self.page.max_concurrent_tasks_spin.setValue(7)
        with patch(
            "jm_downloader.qt.pages.settings_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page.restore_defaults_button.click()

        self.assertEqual(self.store.saved[-1], AppSettings())
        self.assertEqual(
            self.page.max_concurrent_tasks_spin.value(),
            AppSettings().max_concurrent_tasks,
        )
        self.assertTrue(self.page.theme_button(Theme.LIGHT).isChecked())

    def test_save_failure_is_reported_without_publishing_settings(self):
        changes = []
        self.controller.settings_changed.connect(changes.append)
        self.store.save_error = OSError("目录只读")

        with patch(
            "jm_downloader.qt.pages.settings_page.QMessageBox.warning"
        ) as warning:
            self.page.save_button.click()

        self.assertEqual(changes, [])
        warning.assert_called_once_with(self.page, "设置保存失败", "目录只读")
        self.assertTrue(self.page.save_button.isEnabled())
if __name__ == "__main__":
    unittest.main()
