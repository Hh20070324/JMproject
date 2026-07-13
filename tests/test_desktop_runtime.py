import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jm_downloader import desktop_runtime
from jm_downloader.desktop_runtime import SingleInstance, configure_logging
from jm_downloader.settings import AppPaths


class DesktopRuntimeTests(unittest.TestCase):
    def test_configure_logging_applies_selected_level(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            logger = configure_logging(paths, level="ERROR")

            logger.info("hidden info")
            logger.error("visible error")
            for handler in logger.handlers:
                handler.flush()
            content = (paths.logs / "app.log").read_text(encoding="utf-8")
            self.assertNotIn("hidden info", content)
            self.assertIn("visible error", content)

            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_single_instance_detects_existing_mutex_and_activates_window(self):
        kernel32 = Mock()
        kernel32.CreateMutexW.return_value = 123
        kernel32.GetLastError.return_value = SingleInstance.ERROR_ALREADY_EXISTS
        user32 = Mock()
        user32.FindWindowW.return_value = 456
        windll = Mock(kernel32=kernel32, user32=user32)

        with patch.object(desktop_runtime.ctypes, "windll", windll, create=True):
            instance = SingleInstance()
            self.assertFalse(instance.acquire())
            instance.activate_existing_window()
            instance.close()

        kernel32.CreateMutexW.assert_called_once()
        user32.FindWindowW.assert_called_once_with(
            None,
            desktop_runtime.WINDOW_TITLE,
        )
        user32.ShowWindow.assert_called_once_with(456, 9)
        user32.SetForegroundWindow.assert_called_once_with(456)
        kernel32.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
