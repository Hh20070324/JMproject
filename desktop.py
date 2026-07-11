import ctypes
import logging
import threading
import traceback

import webview
from werkzeug.serving import make_server

from jm_downloader.application import create_app
from jm_downloader.library import LibraryError
from jm_downloader.settings import DEFAULT_PATHS


WINDOW_TITLE = "JM 漫画下载器"
MUTEX_NAME = "Local\\JM-Downloader-Desktop"


def configure_logging() -> logging.Logger:
    log_dir = DEFAULT_PATHS.root / "logs"
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


class DesktopServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, application=None):
        self.host = host
        self.app = application or create_app()
        self._server = make_server(host, port, self.app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="desktop-web-server",
            daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class DesktopApi:
    def __init__(self, library):
        self._library = library
        self._window = None

    def set_window(self, window) -> None:
        self._window = window

    def confirm(self, title: str, message: str) -> bool:
        if self._window is None:
            return False
        return bool(self._window.create_confirmation_dialog(title, message))

    def open_library_item(self, album_id: str, kind: str) -> dict:
        try:
            self._library.open_location(str(album_id), str(kind))
            return {"ok": True}
        except LibraryError as error:
            return {"ok": False, "error": str(error)}


def main() -> None:
    logger = configure_logging()
    instance = SingleInstance()
    if not instance.acquire():
        instance.activate_existing_window()
        instance.close()
        return

    try:
        server = DesktopServer()
        server.start()
        logger.info("Desktop server started at %s", server.url)
        print("JM 漫画下载器桌面窗口启动中...")
        print(f"本地服务: {server.url}")
        print("关闭桌面窗口即可退出程序。")

        api = DesktopApi(server.app.config["LIBRARY_SERVICE"])
        window = webview.create_window(
            WINDOW_TITLE,
            server.url,
            js_api=api,
            width=1180,
            height=780,
            min_size=(760, 560),
            background_color="#f3f5f4",
        )
        api.set_window(window)
        manager = server.app.config["TASK_MANAGER"]

        def on_closing():
            if not manager.has_active_tasks():
                return True
            return window.create_confirmation_dialog(
                "下载仍在进行",
                "关闭窗口将终止正在进行的下载，确定要退出吗？",
            )

        window.events.closing += on_closing
        try:
            webview.start(debug=False)
        finally:
            manager.stop_all()
            server.stop()
            logger.info("Desktop application stopped")
    except Exception:
        logger.error("Desktop application crashed\n%s", traceback.format_exc())
        raise
    finally:
        instance.close()


if __name__ == "__main__":
    main()
