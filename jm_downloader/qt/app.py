import argparse
from functools import partial
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle

from ..desktop_runtime import SingleInstance, configure_logging
from ..downloader import DownloadWorker
from ..library import LibraryService
from ..settings import AppPaths, AppSettings, DEFAULT_PATHS, SettingsError
from ..tasks import TaskManager
from .backend_smoke import run_backend_smoke
from .controllers import DownloadController, LibraryController, SettingsController
from .main_window import MainWindow
from .settings_store import SettingsStore, SettingsStoreError
from .theme import ThemeManager, load_stylesheet, resource_path


APPLICATION_NAME = "JM-Downloader"
ORGANIZATION_NAME = "JMProject"


class StartupConfigurationError(RuntimeError):
    pass


def _probe_writable_directory(
    path: Path,
    label: str,
    create: bool = True,
) -> None:
    descriptor = None
    probe_path = None
    try:
        path = Path(path).resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("路径不是文件夹")
        descriptor, probe_name = tempfile.mkstemp(
            dir=path,
            prefix=".jm-write-test-",
        )
        probe_path = Path(probe_name)
    except OSError as error:
        raise StartupConfigurationError(
            f"{label}不可写：{path}\n"
            "请检查路径和写入权限。若这是自定义下载目录，请修正或删除程序目录"
            "下的 settings.json 后重试。"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


def ensure_startup_writable(paths: AppPaths) -> None:
    _probe_writable_directory(paths.root, "程序目录", create=False)


def ensure_runtime_directories_writable(paths: AppPaths) -> None:
    _probe_writable_directory(paths.logs, "日志目录")
    ensure_output_directories_writable(paths)


def ensure_output_directories_writable(paths: AppPaths) -> None:
    _probe_writable_directory(paths.pictures, "图片目录")
    _probe_writable_directory(paths.pdfs, "PDF 目录")


def validate_settings_output_directories(
    base_paths: AppPaths,
    settings: AppSettings,
) -> None:
    ensure_output_directories_writable(base_paths.with_settings(settings))


def _show_startup_error(message: str) -> None:
    QMessageBox.critical(
        None,
        "JM 漫画下载器无法启动",
        str(message),
    )


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


def run_qt_app(
    qt_arguments: list[str],
    smoke_test: bool = False,
    base_paths: AppPaths | None = None,
) -> int:
    base_paths = base_paths or DEFAULT_PATHS
    instance = SingleInstance()
    previous_hook = sys.excepthook
    logger = None
    download_controller = None
    library_controller = None
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(qt_arguments)
        app.setApplicationName(APPLICATION_NAME)
        app.setOrganizationName(ORGANIZATION_NAME)
        app.setQuitOnLastWindowClosed(True)
        if not smoke_test and not instance.acquire():
            instance.activate_existing_window()
            return 0

        try:
            ensure_startup_writable(base_paths)
            settings_store = SettingsStore(base_paths)
            settings = settings_store.load()
            paths = base_paths.with_settings(settings)
            ensure_runtime_directories_writable(paths)
            logger = configure_logging(paths, level=settings.log_level)
        except (
            SettingsError,
            SettingsStoreError,
            StartupConfigurationError,
            OSError,
        ) as error:
            _show_startup_error(str(error))
            return 1

        app.setWindowIcon(
            app.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        )

        theme_manager = ThemeManager(settings.theme)
        theme_manager.apply()
        if not app.styleSheet():
            logger.warning("Qt stylesheet could not be loaded")
        if settings_store.last_recovery_backup is not None:
            logger.warning(
                "Damaged settings were backed up to %s",
                settings_store.last_recovery_backup,
            )

        worker_factory = partial(
            DownloadWorker,
            image_concurrency=settings.image_concurrency,
        )
        manager = TaskManager(
            paths=paths,
            max_concurrent=settings.max_concurrent_tasks,
            worker_factory=worker_factory,
        )
        library = LibraryService(paths)
        download_controller = DownloadController(manager, library)
        library_controller = LibraryController(manager, library)
        settings_controller = SettingsController(
            settings_store,
            settings_validator=partial(
                validate_settings_output_directories,
                base_paths,
            ),
        )
        previous_hook = install_exception_hook(logger)
        window = MainWindow(
            theme_manager,
            download_controller,
            library_controller,
            settings_controller=settings_controller,
            persist_window_state=not smoke_test,
        )
        window.show()
        logger.info("Desktop application started")

        if smoke_test:
            QTimer.singleShot(50, lambda: window.select_page("library"))
            QTimer.singleShot(100, lambda: window.select_page("settings"))
            QTimer.singleShot(150, lambda: window.select_page("downloads"))
            QTimer.singleShot(250, window.close)

        result = app.exec()
        logger.info("Desktop application stopped with exit code %s", result)
        return result
    except Exception:
        if logger is not None:
            logger.error(
                "Desktop application crashed\n%s", traceback.format_exc()
            )
        raise
    finally:
        if library_controller is not None:
            if not library_controller.shutdown(timeout=5.0):
                logger.warning(
                    "Some library workers did not stop before shutdown timeout"
                )
        if download_controller is not None:
            if not download_controller.shutdown(timeout=5.0):
                logger.warning(
                    "Some download workers did not stop before shutdown timeout"
                )
        if logger is not None:
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        sys.excepthook = previous_hook
        instance.close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JM-Downloader desktop application"
    )
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
