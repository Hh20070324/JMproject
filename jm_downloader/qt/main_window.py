from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..desktop_runtime import WINDOW_TITLE
from .pages import DownloadPage, LibraryPage, SettingsPage
from .theme import ThemeManager


class MainWindow(QMainWindow):
    PAGE_ORDER = ("downloads", "library", "settings")

    def __init__(self, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
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
            "downloads": DownloadPage(self),
            "library": LibraryPage(self),
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

    @property
    def current_page(self) -> str:
        return self.PAGE_ORDER[self.stack.currentIndex()]

    def select_page(self, page: str) -> None:
        if page not in self._pages:
            raise ValueError(f"Unknown page: {page}")
        self.stack.setCurrentWidget(self._pages[page])
        self._nav_buttons[page].setChecked(True)

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
