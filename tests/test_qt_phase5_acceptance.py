from dataclasses import replace
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, call, patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jm_downloader import desktop_runtime
from jm_downloader.desktop_runtime import SingleInstance
from jm_downloader.downloader import DownloadWorker
from jm_downloader.library import LibraryService
from jm_downloader.qt import app as qt_app
from jm_downloader.qt.controllers.settings_controller import SettingsController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.qt.theme import Theme, ThemeManager
from jm_downloader.settings import AppPaths, AppSettings
from jm_downloader.task_store import TaskStore


class PhaseFiveAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["phase-five-acceptance-tests"]
        )

    def test_saved_settings_are_reloaded_and_wired_on_each_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-app"
            root.mkdir()
            base_paths = AppPaths(root)
            expected_settings = AppSettings(
                pictures_directory="data/Pictures",
                pdf_directory="data/PDFs",
                max_concurrent_tasks=6,
                image_concurrency=31,
                log_level="ERROR",
                window_width=1280,
                window_height=800,
                startup_page="library",
                theme="dark",
            )
            SettingsStore(base_paths).save(expected_settings)
            expected_paths = base_paths.with_settings(expected_settings)

            fake_application = MagicMock()
            fake_application.styleSheet.return_value = "loaded stylesheet"
            fake_application.exec.return_value = 0
            fake_instance = MagicMock()
            fake_instance.acquire.return_value = True
            fake_download_controller = MagicMock()
            fake_download_controller.shutdown.return_value = True
            fake_library_controller = MagicMock()
            fake_library_controller.shutdown.return_value = True
            fake_account_controller = MagicMock()
            fake_favorites_controller = MagicMock()
            fake_logger = logging.Logger("phase-five-startup-test")

            with (
                patch.object(
                    qt_app,
                    "QApplication",
                    return_value=fake_application,
                ) as application_class,
                patch.object(
                    qt_app.QGuiApplication,
                    "setHighDpiScaleFactorRoundingPolicy",
                ),
                patch.object(
                    qt_app,
                    "SingleInstance",
                    return_value=fake_instance,
                ),
                patch.object(
                    qt_app,
                    "configure_logging",
                    return_value=fake_logger,
                ) as configure_logging,
                patch.object(qt_app, "ThemeManager") as theme_manager_class,
                patch.object(qt_app, "TaskManager") as task_manager_class,
                patch.object(qt_app, "LibraryService") as library_service_class,
                patch.object(
                    qt_app,
                    "DownloadController",
                    return_value=fake_download_controller,
                ),
                patch.object(
                    qt_app,
                    "LibraryController",
                    return_value=fake_library_controller,
                ),
                patch.object(qt_app, "AccountService") as account_service_class,
                patch.object(qt_app, "FavoritesService") as favorites_service_class,
                patch.object(
                    qt_app,
                    "AccountController",
                    return_value=fake_account_controller,
                ) as account_controller_class,
                patch.object(
                    qt_app,
                    "FavoritesController",
                    return_value=fake_favorites_controller,
                ) as favorites_controller_class,
                patch.object(qt_app, "MainWindow") as main_window_class,
                patch.object(
                    qt_app,
                    "install_exception_hook",
                    return_value=sys.excepthook,
                ),
            ):
                for _restart in range(2):
                    self.assertEqual(
                        qt_app.run_qt_app(
                            ["phase-five-startup-test"],
                            base_paths=base_paths,
                        ),
                        0,
                    )

            self.assertEqual(
                configure_logging.call_args_list,
                [
                    call(expected_paths, level="ERROR"),
                    call(expected_paths, level="ERROR"),
                ],
            )
            self.assertEqual(application_class.call_count, 2)
            self.assertEqual(
                theme_manager_class.call_args_list,
                [call("dark"), call("dark")],
            )
            self.assertEqual(task_manager_class.call_count, 2)
            for manager_call in task_manager_class.call_args_list:
                self.assertEqual(manager_call.kwargs["paths"], expected_paths)
                self.assertEqual(manager_call.kwargs["max_concurrent"], 6)
                task_store = manager_call.kwargs["task_store"]
                self.assertIsInstance(task_store, TaskStore)
                self.assertEqual(task_store.paths, expected_paths)
                worker_factory = manager_call.kwargs["worker_factory"]
                self.assertIs(worker_factory.func, DownloadWorker)
                self.assertEqual(
                    worker_factory.keywords["image_concurrency"],
                    31,
                )

            self.assertEqual(
                library_service_class.call_args_list,
                [call(expected_paths), call(expected_paths)],
            )
            self.assertEqual(
                account_service_class.call_args_list,
                [call(paths=expected_paths), call(paths=expected_paths)],
            )
            self.assertEqual(account_controller_class.call_count, 2)
            self.assertEqual(favorites_service_class.call_count, 2)
            self.assertEqual(favorites_controller_class.call_count, 2)
            self.assertEqual(main_window_class.call_count, 2)
            for window_call in main_window_class.call_args_list:
                controller = window_call.kwargs["settings_controller"]
                self.assertEqual(controller.settings, expected_settings)
                self.assertTrue(window_call.kwargs["persist_window_state"])

            invalid_directory = root / "not-a-directory"
            invalid_directory.write_text("occupied", encoding="utf-8")
            controller = main_window_class.call_args_list[-1].kwargs[
                "settings_controller"
            ]
            failures = []
            controller.save_failed.connect(failures.append)
            self.assertFalse(
                controller.save(
                    replace(
                        expected_settings,
                        pictures_directory=str(invalid_directory),
                    )
                )
            )
            self.assertIn("图片目录不可写", failures[-1])
            self.assertEqual(SettingsStore(base_paths).load(), expected_settings)

            self.assertEqual(fake_instance.acquire.call_count, 2)
            self.assertEqual(fake_instance.close.call_count, 2)
            self.assertTrue(expected_paths.pictures.is_dir())
            self.assertTrue(expected_paths.pdfs.is_dir())

    def test_window_state_and_startup_page_survive_a_real_ui_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            initial = AppSettings(
                window_width=800,
                window_height=600,
                startup_page="library",
                theme="dark",
            )
            SettingsStore(paths).save(initial)

            first_controller = SettingsController(SettingsStore(paths))
            first_theme = ThemeManager(first_controller.settings.theme)
            first_theme.apply()
            first_window = MainWindow(
                first_theme,
                settings_controller=first_controller,
            )
            first_window.setAttribute(
                Qt.WidgetAttribute.WA_DontShowOnScreen,
                True,
            )
            first_window.show()
            self.app.processEvents()

            self.assertEqual(first_window.current_page, "library")
            self.assertEqual(first_theme.theme, Theme.DARK)
            first_window.resize(820, 620)
            self.app.processEvents()
            saved_size = (first_window.width(), first_window.height())
            self.assertTrue(first_window.close())
            self.app.processEvents()

            restored = SettingsStore(paths).load()
            self.assertEqual(
                (restored.window_width, restored.window_height),
                saved_size,
            )
            self.assertEqual(restored.startup_page, "library")
            self.assertEqual(restored.theme, "dark")

            second_controller = SettingsController(SettingsStore(paths))
            second_theme = ThemeManager(second_controller.settings.theme)
            second_window = MainWindow(
                second_theme,
                settings_controller=second_controller,
                persist_window_state=False,
            )
            second_window.setAttribute(
                Qt.WidgetAttribute.WA_DontShowOnScreen,
                True,
            )
            second_window.show()
            self.app.processEvents()

            self.assertEqual(second_window.current_page, "library")
            self.assertEqual(second_theme.theme, Theme.DARK)
            self.assertEqual(
                (second_window.width(), second_window.height()),
                saved_size,
            )

            second_window.close()
            first_window.deleteLater()
            second_window.deleteLater()
            first_controller.deleteLater()
            second_controller.deleteLater()
            ThemeManager(Theme.LIGHT).apply()
            self.app.processEvents()

    def test_relative_library_paths_follow_a_moved_portable_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            original_root = container / "original"
            original_root.mkdir()
            original_paths = AppPaths(original_root)
            settings = AppSettings(
                pictures_directory="userdata/Pictures",
                pdf_directory="userdata/PDFs",
            )
            SettingsStore(original_paths).save(settings)
            original_runtime = original_paths.with_settings(settings)

            image_path = (
                original_runtime.pictures / "1449491" / "chapter-1" / "1.jpg"
            )
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"offline image fixture")
            pdf_path = original_runtime.pdfs / "1449491.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-1.4\noffline fixture")

            moved_root = container / "moved"
            original_root.rename(moved_root)
            self.assertFalse(original_root.exists())

            moved_paths = AppPaths(moved_root)
            moved_settings = SettingsStore(moved_paths).load()
            moved_runtime = moved_paths.with_settings(moved_settings)
            library = LibraryService(moved_runtime)
            items = library.list_items()

            self.assertEqual(moved_settings, settings)
            self.assertEqual(
                moved_runtime.pictures,
                (moved_root / "userdata/Pictures").resolve(),
            )
            self.assertEqual(
                moved_runtime.pdfs,
                (moved_root / "userdata/PDFs").resolve(),
            )
            self.assertEqual(moved_runtime.logs, moved_root / "logs")
            self.assertEqual([item.album_id for item in items], ["1449491"])
            self.assertTrue(
                items[0].preview_path.is_relative_to(moved_root.resolve())
            )
            self.assertTrue(items[0].pdf_path.is_relative_to(moved_root.resolve()))

    @unittest.skipUnless(
        os.name == "nt" and hasattr(desktop_runtime.ctypes, "windll"),
        "Windows named mutex integration test",
    )
    def test_named_mutex_enforces_single_instance_between_real_handles(self):
        unique_name = f"Local\\JM-Downloader-Test-{uuid.uuid4().hex}"
        first = SingleInstance()
        second = SingleInstance()
        try:
            with patch.object(desktop_runtime, "MUTEX_NAME", unique_name):
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
        finally:
            second.close()
            first.close()


if __name__ == "__main__":
    unittest.main()
