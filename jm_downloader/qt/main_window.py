from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..desktop_runtime import WINDOW_TITLE
from ..models import (
    ReaderChapterDownloadState,
    ReaderHistoryEntry,
    ReaderSource,
    SearchResultSnapshot,
)
from ..reader import ReaderHistoryStore
from .icons import svg_icon
from .controllers.account_controller import AccountController
from .controllers.chapter_catalog_controller import ChapterCatalogController
from .controllers.download_controller import DownloadController
from .controllers.favorites_controller import FavoritesController
from .controllers.library_controller import LibraryController
from .controllers.reader_controller import ReaderController
from .controllers.reader_download_controller import ReaderDownloadController
from .controllers.search_controller import SearchController
from .controllers.settings_controller import SettingsController
from .pages import (
    DownloadPage,
    FavoritesPage,
    LibraryPage,
    SettingsPage,
)
from .reader_window import ReaderWindow
from .theme import ThemeManager


class MainWindow(QMainWindow):
    PAGE_ORDER = ("downloads", "favorites", "library", "settings")

    def __init__(
        self,
        theme_manager: ThemeManager,
        download_controller: DownloadController | None = None,
        library_controller: LibraryController | None = None,
        parent=None,
        settings_controller: SettingsController | None = None,
        search_controller: SearchController | None = None,
        account_controller: AccountController | None = None,
        favorites_controller: FavoritesController | None = None,
        chapter_catalog_controller: ChapterCatalogController | None = None,
        reader_controller: ReaderController | None = None,
        reader_history_store: ReaderHistoryStore | None = None,
        persist_window_state: bool = True,
    ):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.download_controller = download_controller
        self.library_controller = library_controller
        self.settings_controller = settings_controller
        self.search_controller = search_controller
        self.account_controller = account_controller
        self.favorites_controller = favorites_controller
        self.chapter_catalog_controller = chapter_catalog_controller
        self.reader_controller = reader_controller
        self.reader_history_store = reader_history_store
        self.reader_window: ReaderWindow | None = None
        self.reader_download_controller: ReaderDownloadController | None = None
        self._reader_shutdown_requested = False
        self._persist_window_state = bool(persist_window_state)
        self._shutdown_pending = False
        self._shutdown_complete = False
        self.setObjectName("mainWindow")
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(760, 520)
        settings = (
            self.settings_controller.settings
            if self.settings_controller is not None
            else None
        )
        self._last_settings = settings
        initial_size = self._constrained_window_size(
            settings.window_width if settings is not None else 1100,
            settings.window_height if settings is not None else 720,
        )
        self.resize(*initial_size)

        app = QApplication.instance()
        if app is not None:
            icon = app.windowIcon()
            if icon.isNull():
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
            self.setWindowIcon(icon)

        root = QWidget(self)
        root.setObjectName("windowRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        self._navigation = QButtonGroup(self)
        self._navigation.setExclusive(True)
        self._nav_buttons = {}
        self._pages = {
            "downloads": DownloadPage(
                download_controller,
                self,
                search_controller=search_controller,
                favorites_controller=favorites_controller,
                chapter_catalog_controller=chapter_catalog_controller,
                reader_history_store=reader_history_store,
                reader_available=reader_controller is not None,
            ),
            "favorites": FavoritesPage(
                account_controller,
                self,
                favorites_controller=favorites_controller,
                download_controller=download_controller,
                cover_service=(
                    search_controller.service
                    if search_controller is not None
                    else None
                ),
                chapter_catalog_controller=chapter_catalog_controller,
                reader_available=reader_controller is not None,
            ),
            "library": LibraryPage(
                library_controller,
                self,
                chapter_catalog_controller=chapter_catalog_controller,
                settings_controller=settings_controller,
            ),
            "settings": SettingsPage(
                theme_manager,
                self,
                settings_controller=settings_controller,
                search_controller=search_controller,
            ),
        }
        if reader_controller is not None:
            if (
                download_controller is not None
                and callable(
                    getattr(
                        getattr(download_controller, "library", None),
                        "completed_chapter_ids",
                        None,
                    )
                )
            ):
                self.reader_download_controller = ReaderDownloadController(
                    download_controller.library.completed_chapter_ids,
                    download_controller,
                    self,
                )
            self.reader_window = ReaderWindow(
                reader_controller,
                self,
                settings_controller=settings_controller,
                download_state_controller=self.reader_download_controller,
                persist_geometry=persist_window_state,
            )
            self._pages["reader"] = self.reader_window.page

        root_layout.addWidget(self._create_sidebar(root))

        self.stack = QStackedWidget(root)
        self.stack.setObjectName("pageStack")
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        for key in self.PAGE_ORDER:
            self.stack.addWidget(self._pages[key])
        root_layout.addWidget(self.stack, 1)

        startup_page = (
            settings.startup_page if settings is not None else "downloads"
        )
        self.select_page(startup_page)
        self._center_on_screen()

        if self.download_controller is not None:
            self.download_controller.shutdown_finished.connect(
                self._finish_download_shutdown
            )
        if self.settings_controller is not None:
            self.settings_controller.settings_changed.connect(
                self._apply_settings
            )
        self._pages["favorites"].view_task_requested.connect(
            self._show_download_task
        )
        self._pages["library"].view_task_requested.connect(
            self._show_download_task
        )
        if "reader" in self._pages:
            self._pages["downloads"].read_requested.connect(
                self._open_reader
            )
            self._pages["downloads"].reading_history_requested.connect(
                self._open_reader_history
            )
            self._pages["favorites"].read_requested.connect(
                self._open_reader
            )
            self._pages["reader"].download_chapter_requested.connect(
                self._download_reader_chapter
            )

    @property
    def current_page(self) -> str:
        current = self.stack.currentWidget()
        for key, page in self._pages.items():
            if page is current:
                return key
        return "downloads"

    def select_page(self, page: str) -> None:
        if page not in self.PAGE_ORDER:
            raise ValueError(f"Unknown page: {page}")
        self.stack.setCurrentWidget(self._pages[page])
        self._nav_buttons[page].setChecked(True)
        activate = getattr(self._pages[page], "activate", None)
        if activate is not None:
            activate()

    def navigation_button(self, page: str) -> QToolButton:
        return self._nav_buttons[page]

    def page(self, page: str) -> QWidget:
        return self._pages[page]

    def _create_sidebar(self, parent: QWidget) -> QWidget:
        sidebar = QWidget(parent)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 22)
        layout.setSpacing(8)

        brand = QWidget(sidebar)
        brand.setObjectName("brand")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 18)
        brand_layout.setSpacing(10)

        mark = QLabel("JM", brand)
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(38, 38)
        brand_layout.addWidget(mark)

        name = QLabel("JM Downloader", brand)
        name.setObjectName("brandName")
        brand_layout.addWidget(name, 1)
        layout.addWidget(brand)

        entries = (
            (
                "downloads",
                "搜索与下载",
                svg_icon("search", "#ffffff"),
            ),
            (
                "favorites",
                "我的收藏",
                svg_icon("bookmark", "#ffffff"),
            ),
            (
                "library",
                "本地漫画库",
                svg_icon("folder", "#ffffff"),
            ),
            (
                "settings",
                "设置",
                svg_icon("settings", "#ffffff"),
            ),
        )
        for index, (key, text, icon) in enumerate(entries):
            button = QToolButton(sidebar)
            button.setObjectName("navButton")
            button.setProperty("page", key)
            button.setText(text)
            button.setIcon(icon)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setFixedHeight(44)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(lambda checked=False, page=key: self.select_page(page))
            self._navigation.addButton(button, index)
            self._nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        return sidebar

    def _center_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.library_controller is not None
            and self.library_controller.has_pending_mutations()
        ):
            QMessageBox.information(
                self,
                "本地库操作进行中",
                "请等待 PDF 生成或删除操作完成后再退出。",
            )
            event.ignore()
            return

        controller = self.download_controller
        if controller is None or self._shutdown_complete:
            self._request_reader_shutdown()
            self._dispose_search()
            self._save_window_size()
            super().closeEvent(event)
            return
        if self._shutdown_pending:
            event.ignore()
            return
        if not controller.has_active_tasks():
            self._request_reader_shutdown()
            self._dispose_search()
            self._save_window_size()
            super().closeEvent(event)
            return

        answer = QMessageBox.question(
            self,
            "下载仍在进行",
            "关闭窗口会保存未完成任务并将其暂停。下次启动后可手动继续，确定要退出吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        event.ignore()
        self._shutdown_pending = True
        self.setEnabled(False)
        self._request_reader_shutdown()
        controller.begin_shutdown(timeout=5.0)

    def _apply_settings(self, settings) -> None:
        previous = self._last_settings
        self._last_settings = settings
        self.theme_manager.set_theme(settings.theme)
        target_size = self._constrained_window_size(
            settings.window_width,
            settings.window_height,
        )
        if (self.width(), self.height()) != target_size:
            self.resize(*target_size)
        if (
            previous is not None
            and self.download_controller is not None
            and (
                previous.pictures_directory != settings.pictures_directory
                or previous.pdf_directory != settings.pdf_directory
            )
            and self.download_controller.list_tasks()
        ):
            QMessageBox.information(
                self,
                "下载目录已更新",
                "现有任务仍使用创建时的原目录；重启后新建的任务使用新目录。",
            )

    def _constrained_window_size(
        self,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return width, height
        available = screen.availableGeometry()
        return min(width, available.width()), min(height, available.height())

    def _save_window_size(self) -> None:
        if not self._persist_window_state or self.settings_controller is None:
            return
        current = self.settings_controller.settings
        size = (self.width(), self.height())
        if size == (current.window_width, current.window_height):
            return
        self.settings_controller.save(
            replace(
                current,
                window_width=max(self.minimumWidth(), size[0]),
                window_height=max(self.minimumHeight(), size[1]),
            )
        )

    def _finish_download_shutdown(self, completed: bool) -> None:
        self._shutdown_pending = False
        self._shutdown_complete = True
        self.setEnabled(True)
        if not completed:
            QMessageBox.warning(
                self,
                "下载尚未完全停止",
                "部分后台任务未能及时停止。任务已保存，下次启动后会以暂停状态恢复。",
            )
        self.close()

    def _show_download_task(self, album_id: str) -> None:
        self.select_page("downloads")
        self._pages["downloads"].show_task(album_id)

    def _open_reader(
        self,
        snapshot: SearchResultSnapshot,
        source: ReaderSource,
    ) -> None:
        if self.reader_window is None:
            return
        if not isinstance(snapshot, SearchResultSnapshot):
            return
        if not isinstance(source, ReaderSource):
            source = ReaderSource.SEARCH
        if (
            self.reader_window.has_session
            and self.reader_window.session_album_id == snapshot.album_id
        ):
            self.reader_window.activate_session()
            return
        if (
            self.reader_window.has_session
            and not self._confirm_reader_reuse(snapshot.title)
        ):
            return
        if self.reader_window.has_session:
            self.reader_window.end_session()
        history = (
            self.reader_history_store.find(snapshot.album_id)
            if self.reader_history_store is not None
            else None
        )
        preferred_photo_id = history.photo_id if history else None
        preferred_page = history.page_number if history else 1
        reader = self._pages["reader"]
        self.reader_window.begin_session(
            snapshot.album_id,
            snapshot.title,
        )
        if snapshot.chapter_catalog is not None:
            reader.open_catalog(
                snapshot.chapter_catalog,
                source=source,
                photo_id=preferred_photo_id,
                page_number=preferred_page,
            )
        else:
            reader.open_album(
                snapshot.album_id,
                title=snapshot.title,
                source=source,
                preferred_photo_id=preferred_photo_id,
                preferred_page=preferred_page,
            )

    def _open_reader_history(self, entry: ReaderHistoryEntry) -> None:
        if (
            self.reader_window is None
            or not isinstance(entry, ReaderHistoryEntry)
        ):
            return
        if (
            self.reader_window.has_session
            and self.reader_window.session_album_id == entry.album_id
        ):
            self.reader_window.activate_session()
            return
        if (
            self.reader_window.has_session
            and not self._confirm_reader_reuse(entry.title)
        ):
            return
        if self.reader_window.has_session:
            self.reader_window.end_session()
        reader = self._pages["reader"]
        self.reader_window.begin_session(entry.album_id, entry.title)
        reader.open_album(
            entry.album_id,
            title=entry.title,
            source=ReaderSource.HISTORY,
            preferred_photo_id=entry.photo_id,
            preferred_page=entry.page_number,
        )

    def _confirm_reader_reuse(self, next_title: str | None) -> bool:
        if self.reader_window is None:
            return False
        dialog = QMessageBox(self.reader_window)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("切换在线阅读")
        target = (
            f"《{next_title.strip()}》"
            if isinstance(next_title, str) and next_title.strip()
            else "另一部漫画"
        )
        dialog.setText(f"当前阅读窗口将切换到{target}。")
        dialog.setInformativeText(
            "当前阅读进度会先保存，现有阅读窗口将被复用。"
        )
        switch_button = dialog.addButton(
            "切换阅读",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_button = dialog.addButton(
            "取消",
            QMessageBox.ButtonRole.RejectRole,
        )
        dialog.setDefaultButton(cancel_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        return dialog.clickedButton() is switch_button

    def _download_reader_chapter(self, photo_id: str) -> None:
        if self.download_controller is None or self.reader_window is None:
            return
        reader = self._pages["reader"]
        if (
            self.reader_download_controller is not None
            and reader.download_state
            is not ReaderChapterDownloadState.AVAILABLE
        ):
            return
        album_id = reader.current_album_id
        if album_id is None:
            return
        outcome = self.download_controller.add_task_batch(
            album_id,
            (photo_id,),
            (),
        )
        if outcome.snapshots:
            reader.show_notice("当前章节已加入正式下载任务")
            return
        if self.reader_download_controller is not None:
            self.reader_download_controller.refresh_tasks()
        message = outcome.error or "\n".join(
            issue.message for issue in outcome.issues
        )
        QMessageBox.warning(
            self.reader_window,
            "无法创建下载任务",
            message or "当前章节暂时无法加入下载任务。",
        )

    def _request_reader_shutdown(self) -> None:
        if (
            self.reader_controller is None
            or self._reader_shutdown_requested
        ):
            return
        if self.reader_window is not None:
            self.reader_window.close()
        self._reader_shutdown_requested = True
        self.reader_controller.request_shutdown(timeout=5.0)

    def _dispose_search(self) -> None:
        download_page = self._pages.get("downloads")
        dispose_page = getattr(download_page, "dispose", None)
        if dispose_page is not None:
            dispose_page()
        if self.search_controller is not None:
            self.search_controller.dispose()
        if self.chapter_catalog_controller is not None:
            self.chapter_catalog_controller.dispose()
        favorites_page = self._pages.get("favorites")
        dispose_favorites_page = getattr(favorites_page, "dispose", None)
        if dispose_favorites_page is not None:
            dispose_favorites_page()
        if self.favorites_controller is not None:
            self.favorites_controller.dispose()
        if self.account_controller is not None:
            self.account_controller.dispose()
        if self.reader_download_controller is not None:
            self.reader_download_controller.dispose()
