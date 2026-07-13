import argparse
import logging
import sys
import traceback

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle

from ..desktop_runtime import SingleInstance, configure_logging
from ..library import LibraryService
from ..tasks import TaskManager
from .backend_smoke import run_backend_smoke
from .controllers import DownloadController
from .main_window import MainWindow
from .theme import ThemeManager, load_stylesheet, resource_path


APPLICATION_NAME = "JM-Downloader"
ORGANIZATION_NAME = "JMProject"

def install_exception_hook(logger: logging.Logger):
    previous_hook = sys.excepthook

    def handle_exception(error_type, error, error_traceback):
        details = "".join(
            traceback.format_exception(error_type, error, error_traceback)
        )
        logger.critical("Unhandled Qt exception\n%s", details)
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                "程序发生错误",
                "程序遇到未处理的错误，详细信息已写入 logs/app.log。",
            )

    sys.excepthook = handle_exception
    return previous_hook


def run_qt_app(qt_arguments: list[str], smoke_test: bool = False) -> int:
    logger = configure_logging()
    instance = SingleInstance()
    if not smoke_test and not instance.acquire():
        instance.activate_existing_window()
        instance.close()
        return 0

    previous_hook = sys.excepthook
    download_controller = None
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(qt_arguments)
        app.setApplicationName(APPLICATION_NAME)
        app.setOrganizationName(ORGANIZATION_NAME)
        app.setQuitOnLastWindowClosed(True)
        app.setWindowIcon(
            app.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        )

        theme_manager = ThemeManager()
        theme_manager.apply()
        if not app.styleSheet():
            logger.warning("Qt stylesheet could not be loaded")

        manager = TaskManager()
        library = LibraryService()
        download_controller = DownloadController(manager, library)
        previous_hook = install_exception_hook(logger)
        window = MainWindow(theme_manager, download_controller)
        window.show()
        logger.info("Qt prototype started")

        if smoke_test:
            QTimer.singleShot(50, lambda: window.select_page("library"))
            QTimer.singleShot(100, lambda: window.select_page("settings"))
            QTimer.singleShot(150, lambda: window.select_page("downloads"))
            QTimer.singleShot(250, window.close)

        result = app.exec()
        logger.info("Qt prototype stopped with exit code %s", result)
        return result
    except Exception:
        logger.error("Qt prototype crashed\n%s", traceback.format_exc())
        raise
    finally:
        if download_controller is not None:
            if not download_controller.shutdown(timeout=5.0):
                logger.warning(
                    "Some download workers did not stop before shutdown timeout"
                )
        sys.excepthook = previous_hook
        instance.close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JM-Downloader Qt prototype")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--backend-smoke-test", action="store_true")
    parsed, qt_arguments = parser.parse_known_args(arguments)
    if parsed.backend_smoke_test:
        try:
            run_backend_smoke()
        except Exception:
            traceback.print_exc()
            return 1
        return 0
    return run_qt_app([sys.argv[0], *qt_arguments], smoke_test=parsed.smoke_test)
