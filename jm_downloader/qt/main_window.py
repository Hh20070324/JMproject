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
from .controllers.download_controller import DownloadController
from .controllers.library_controller import LibraryController
from .pages import DownloadPage, LibraryPage, SettingsPage
from .theme import ThemeManager


class MainWindow(QMainWindow):
    PAGE_ORDER = ("downloads", "library", "settings")

    def __init__(
        self,
        theme_manager: ThemeManager,
        download_controller: DownloadController | None = None,
        library_controller: LibraryController | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.download_controller = download_controller
        self.library_controller = library_controller
        self._shutdown_pending = False
        self._shutdown_complete = False
        self.setObjectName("mainWindow")
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1100, 720)
        self.setMinimumSize(760, 520)

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
            "downloads": DownloadPage(download_controller, self),
            "library": LibraryPage(library_controller, self),
            "settings": SettingsPage(theme_manager, self),
        }

        root_layout.addWidget(self._create_sidebar(root))

        self.stack = QStackedWidget(root)
        self.stack.setObjectName("pageStack")
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        for key in self.PAGE_ORDER:
            self.stack.addWidget(self._pages[key])
        root_layout.addWidget(self.stack, 1)

        self.select_page("downloads")
        self._center_on_screen()

        if self.download_controller is not None:
            self.download_controller.shutdown_finished.connect(
                self._finish_download_shutdown
            )

    @property
    def current_page(self) -> str:
        return self.PAGE_ORDER[self.stack.currentIndex()]

    def select_page(self, page: str) -> None:
        if page not in self._pages:
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

        style = self.style()
        entries = (
            (
                "downloads",
                "下载任务",
                style.standardIcon(QStyle.StandardPixmap.SP_ArrowDown),
            ),
            (
                "library",
                "本地漫画库",
                style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon),
            ),
            (
                "settings",
                "设置",
                style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
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
            super().closeEvent(event)
            return
        if self._shutdown_pending:
            event.ignore()
            return
        if not controller.has_active_tasks():
            super().closeEvent(event)
            return

        answer = QMessageBox.question(
            self,
            "下载仍在进行",
            "关闭窗口将停止正在进行的下载，确定要退出吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        event.ignore()
        self._shutdown_pending = True
        self.setEnabled(False)
        controller.begin_shutdown(timeout=5.0)

    def _finish_download_shutdown(self, completed: bool) -> None:
        self._shutdown_pending = False
        self._shutdown_complete = True
        self.setEnabled(True)
        if not completed:
            QMessageBox.warning(
                self,
                "下载尚未完全停止",
                "部分后台任务未能及时停止，可能留下未完成的文件。",
            )
        self.close()
