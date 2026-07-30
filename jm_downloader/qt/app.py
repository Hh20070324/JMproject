import argparse
from dataclasses import replace
from functools import partial
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback

from PySide6.QtCore import QMetaObject, QTimer, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from ..desktop_runtime import SingleInstance, configure_logging
from ..account import AccountError, AccountService
from ..credentials import CredentialStore
from ..downloader import DownloadWorker
from ..favorites import FavoritesService
from ..jmcomic_logging import install_safe_jmcomic_logging
from ..library import LibraryService
from ..local_reader import LocalReaderService
from ..option_config import ApiRouteState, QueryEngineState
from ..protected_store import ProtectedStore
from ..reader import (
    ReaderDiskCache,
    ReaderHistoryStore,
    ReaderService,
    ReaderTempError,
)
from ..search import SearchService
from ..search_history import SearchHistoryStore
from ..settings import AppPaths, AppSettings, DEFAULT_PATHS, SettingsError
from ..task_store import TaskStore, TaskStoreError
from ..tasks import TaskManager
from .backend_smoke import run_backend_smoke
from .controllers import (
    AccountController,
    ChapterCatalogController,
    DownloadController,
    FavoritesController,
    LibraryController,
    ReaderController,
    SearchController,
    SettingsController,
)
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
    _probe_writable_directory(paths.pdfs, "打包产物目录")


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
    install_safe_jmcomic_logging()
    base_paths = base_paths or DEFAULT_PATHS
    instance = SingleInstance()
    previous_hook = sys.excepthook
    logger = None
    download_controller = None
    library_controller = None
    search_controller = None
    chapter_catalog_controller = None
    account_controller = None
    favorites_controller = None
    reader_controller = None
    task_store = None
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

        application_icon = QIcon(str(resource_path("app.ico")))
        if application_icon.isNull():
            logger.warning("Application icon could not be loaded")
        else:
            app.setWindowIcon(application_icon)

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
            multi_chapter_download_behavior=(
                settings.multi_chapter_download_behavior
            ),
        )
        try:
            legacy_config = replace(
                settings.task_config(),
                download_engine="sync",
                api_route="auto",
                package_format="pdf",
                image_format="jpg",
            )
            task_store = TaskStore(
                paths,
                legacy_task_config=legacy_config,
            )
            manager = TaskManager(
                paths=paths,
                max_concurrent=settings.max_concurrent_tasks,
                worker_factory=worker_factory,
                task_store=task_store,
                new_task_config=settings.task_config(),
            )
        except TaskStoreError as error:
            _show_startup_error(str(error))
            return 1
        if task_store.last_recovery_backup is not None:
            logger.warning(
                "Damaged task records were backed up to %s",
                task_store.last_recovery_backup,
            )
        library = LibraryService(paths)
        download_controller = DownloadController(manager, library)
        library_controller = LibraryController(manager, library)
        api_route_state = ApiRouteState(settings.api_route)
        query_engine_state = QueryEngineState(settings.query_engine)
        search_service = SearchService(
            paths=paths,
            api_route_provider=api_route_state.get,
            query_engine_provider=query_engine_state.get,
        )
        search_controller = SearchController(
            search_service,
            history_store=SearchHistoryStore(
                ProtectedStore.search_history(paths)
            ),
        )
        chapter_catalog_controller = ChapterCatalogController(
            search_service,
            downloaded_detector=library.completed_chapter_ids,
        )
        account_service = AccountService(
            paths=paths,
            api_route_provider=api_route_state.get,
        )
        account_controller = AccountController(
            account_service,
            credential_store=CredentialStore(
                ProtectedStore.credentials(paths)
            ),
            remember_credentials=settings.remember_credentials,
        )
        favorites_controller = FavoritesController(
            FavoritesService(
                account_service,
                paths=paths,
                api_route_provider=api_route_state.get,
                query_engine_provider=query_engine_state.get,
            ),
            account_controller,
        )
        reader_history_store = ReaderHistoryStore(
            ProtectedStore.reading_history(paths)
        )

        def reader_session_cookies():
            try:
                return account_service.current_session().cookie_dict()
            except AccountError:
                return None

        def mark_reader_session_expired():
            QMetaObject.invokeMethod(
                account_controller,
                "mark_expired",
                Qt.ConnectionType.QueuedConnection,
            )

        try:
            reader_service = ReaderService(
                option_file=paths.option_file,
                disk_cache=ReaderDiskCache(paths.reader_temp),
                api_route_provider=api_route_state.get,
                session_cookie_provider=reader_session_cookies,
                session_expired_callback=mark_reader_session_expired,
            )
            reader_controller = ReaderController(
                reader_service,
                local_service=LocalReaderService(paths),
                history_store=reader_history_store,
            )
        except ReaderTempError as error:
            _show_startup_error(str(error))
            return 1
        settings_controller = SettingsController(
            settings_store,
            settings_validator=partial(
                validate_settings_output_directories,
                base_paths,
            ),
        )
        settings_controller.settings_changed.connect(
            lambda current: manager.set_new_task_config(
                current.task_config()
            )
        )
        settings_controller.settings_changed.connect(
            lambda current: api_route_state.set(current.api_route)
        )
        settings_controller.settings_changed.connect(
            lambda current: query_engine_state.set(current.query_engine)
        )
        settings_controller.settings_changed.connect(
            lambda current: account_controller.set_remember_credentials(
                current.remember_credentials
            )
        )
        previous_hook = install_exception_hook(logger)
        window = MainWindow(
            theme_manager,
            download_controller,
            library_controller,
            search_controller=search_controller,
            chapter_catalog_controller=chapter_catalog_controller,
            account_controller=account_controller,
            favorites_controller=favorites_controller,
            settings_controller=settings_controller,
            reader_controller=reader_controller,
            reader_history_store=reader_history_store,
            persist_window_state=not smoke_test,
        )
        window.show()
        logger.info("Desktop application started")

        if smoke_test:
            QTimer.singleShot(50, lambda: window.select_page("favorites"))
            QTimer.singleShot(100, lambda: window.select_page("library"))
            QTimer.singleShot(150, lambda: window.select_page("settings"))
            QTimer.singleShot(200, lambda: window.select_page("downloads"))
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
        if reader_controller is not None:
            if not reader_controller.shutdown(timeout=5.0):
                logger.warning(
                    "Online reader worker did not stop before shutdown timeout"
                )
        if chapter_catalog_controller is not None:
            chapter_catalog_controller.dispose()
        if favorites_controller is not None:
            favorites_controller.dispose()
        if account_controller is not None:
            account_controller.dispose()
        if search_controller is not None:
            search_controller.dispose()
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
        elif task_store is not None:
            task_store.close(timeout=5.0)
        if logger is not None:
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        sys.excepthook = previous_hook
        instance.close()


def main(arguments: list[str] | None = None) -> int:
    install_safe_jmcomic_logging()
    parser = argparse.ArgumentParser(
        description="JM-Downloader desktop application"
    )
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--backend-smoke-test", action="store_true")
    parsed, qt_arguments = parser.parse_known_args(arguments)
    if parsed.backend_smoke_test:
        try:
            run_backend_smoke()
        except Exception as error:
            print(
                f"Backend smoke test failed ({type(error).__name__})",
                file=sys.stderr,
            )
            return 1
        return 0
    return run_qt_app([sys.argv[0], *qt_arguments], smoke_test=parsed.smoke_test)
