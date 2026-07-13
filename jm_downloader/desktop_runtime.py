import ctypes
import logging
from ctypes import wintypes

from .settings import AppPaths, DEFAULT_PATHS


WINDOW_TITLE = "JM 漫画下载器"
MUTEX_NAME = "Local\\JM-Downloader-Desktop"


def configure_logging(
    paths: AppPaths = DEFAULT_PATHS,
    level: int | str = "INFO",
) -> logging.Logger:
    selected_level = (
        int(level)
        if isinstance(level, int) and not isinstance(level, bool)
        else getattr(logging, str(level).upper(), logging.INFO)
    )
    log_dir = paths.logs
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jm-downloader")
    logger.setLevel(selected_level)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(
        log_dir / "app.log",
        encoding="utf-8",
    )
    handler.setLevel(selected_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


class SingleInstance:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self):
        self._handle = None

    def acquire(self) -> bool:
        if not hasattr(ctypes, "windll"):
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.argtypes = ()
        kernel32.GetLastError.restype = wintypes.DWORD
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return (
            bool(self._handle)
            and kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS
        )

    def activate_existing_window(self) -> None:
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = (wintypes.HWND, wintypes.INT)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        window = user32.FindWindowW(None, WINDOW_TITLE)
        if window:
            user32.ShowWindow(window, 9)
            user32.SetForegroundWindow(window)

    def close(self) -> None:
        if self._handle and hasattr(ctypes, "windll"):
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self._handle)
            self._handle = None
