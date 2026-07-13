import argparse
import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle

from ..desktop_runtime import SingleInstance, configure_logging
from .main_window import MainWindow


APPLICATION_NAME = "JM-Downloader"
ORGANIZATION_NAME = "JMProject"


def resource_path(filename: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        return Path(bundle_root) / "jm_downloader" / "qt" / "resources" / filename
    return Path(__file__).resolve().parent / "resources" / filename


def load_stylesheet() -> str:
    try:
        return resource_path("styles.qss").read_text(encoding="utf-8")
    except OSError:
        return ""


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

        stylesheet = load_stylesheet()
        if stylesheet:
            app.setStyleSheet(stylesheet)
        else:
            logger.warning("Qt stylesheet could not be loaded")

        previous_hook = install_exception_hook(logger)
        window = MainWindow()
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
        sys.excepthook = previous_hook
        instance.close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JM-Downloader Qt prototype")
    parser.add_argument("--smoke-test", action="store_true")
    parsed, qt_arguments = parser.parse_known_args(arguments)
    return run_qt_app([sys.argv[0], *qt_arguments], smoke_test=parsed.smoke_test)
