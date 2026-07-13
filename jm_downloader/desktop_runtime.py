import ctypes
import logging

from .settings import AppPaths, DEFAULT_PATHS


WINDOW_TITLE = "JM 漫画下载器"
MUTEX_NAME = "Local\\JM-Downloader-Desktop"


def configure_logging(paths: AppPaths = DEFAULT_PATHS) -> logging.Logger:
    log_dir = paths.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "app.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
        encoding="utf-8",
    )
    return logging.getLogger("jm-downloader")


class SingleInstance:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self):
        self._handle = None

    def acquire(self) -> bool:
        if not hasattr(ctypes, "windll"):
            return True
        kernel32 = ctypes.windll.kernel32
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return bool(self._handle) and kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS

    def activate_existing_window(self) -> None:
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        window = user32.FindWindowW(None, WINDOW_TITLE)
        if window:
            user32.ShowWindow(window, 9)
            user32.SetForegroundWindow(window)

    def close(self) -> None:
        if self._handle and hasattr(ctypes, "windll"):
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
