import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jm_downloader import desktop_runtime
from jm_downloader.qt import app as qt_app
from jm_downloader.settings import AppPaths


class QtStartupSafetyTests(unittest.TestCase):
    def test_write_probe_failure_skips_logging_and_shows_critical(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            startup_error = qt_app.StartupConfigurationError(
                "程序目录不可写（测试）"
            )

            fake_application = MagicMock()
            fake_instance = MagicMock()
            with (
                patch.object(qt_app, "DEFAULT_PATHS", paths),
                patch.object(qt_app, "QApplication") as application_class,
                patch.object(
                    qt_app.QGuiApplication,
                    "setHighDpiScaleFactorRoundingPolicy",
                ),
                patch.object(qt_app, "SingleInstance", return_value=fake_instance),
                patch.object(
                    qt_app,
                    "ensure_startup_writable",
                    side_effect=startup_error,
                ) as ensure_writable,
                patch.object(qt_app, "configure_logging") as configure_logging,
                patch.object(qt_app.QMessageBox, "critical") as critical,
            ):
                application_class.return_value = fake_application
                result = qt_app.run_qt_app(["qt-startup-test"], smoke_test=True)

            self.assertNotEqual(result, 0)
            ensure_writable.assert_called_once_with(paths)
            configure_logging.assert_not_called()
            critical.assert_called_once()
            self.assertIn(
                "程序目录不可写（测试）",
                " ".join(str(value) for value in critical.call_args.args),
            )
            fake_instance.close.assert_called_once()

    def test_write_probe_wraps_unusable_portable_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "not-a-directory"
            root.write_text("occupied", encoding="utf-8")

            with self.assertRaises(qt_app.StartupConfigurationError):
                qt_app.ensure_startup_writable(AppPaths(root))


class LoggingConfigurationTests(unittest.TestCase):
    def test_configured_log_level_is_effective(self):
        root_logger = logging.getLogger()
        app_logger = logging.getLogger("jm-downloader")
        original_root_handlers = list(root_logger.handlers)
        original_app_handlers = list(app_logger.handlers)
        original_root_level = root_logger.level
        original_app_level = app_logger.level
        original_propagate = app_logger.propagate

        for handler in original_root_handlers:
            root_logger.removeHandler(handler)
        for handler in original_app_handlers:
            app_logger.removeHandler(handler)

        temp_dir = tempfile.TemporaryDirectory()
        try:
            paths = AppPaths(Path(temp_dir.name))
            logger = desktop_runtime.configure_logging(
                paths,
                level=logging.DEBUG,
            )
            logger.debug("debug-level-test")

            generated_handlers = {
                *root_logger.handlers,
                *app_logger.handlers,
            }
            for handler in generated_handlers:
                handler.flush()

            self.assertTrue(logger.isEnabledFor(logging.DEBUG))
            log_path = paths.root / "logs" / "app.log"
            self.assertTrue(log_path.is_file())
            self.assertIn(
                "debug-level-test",
                log_path.read_text(encoding="utf-8"),
            )
        finally:
            generated_handlers = {
                *root_logger.handlers,
                *app_logger.handlers,
            }
            for handler in generated_handlers:
                root_logger.removeHandler(handler)
                app_logger.removeHandler(handler)
                handler.close()
            for handler in original_root_handlers:
                root_logger.addHandler(handler)
            for handler in original_app_handlers:
                app_logger.addHandler(handler)
            root_logger.setLevel(original_root_level)
            app_logger.setLevel(original_app_level)
            app_logger.propagate = original_propagate
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
