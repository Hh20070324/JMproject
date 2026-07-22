from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import (
    AccountSnapshot,
    AccountStatus,
    ChapterCatalogSnapshot,
    FavoriteFolderSnapshot,
    FavoritesFilterSnapshot,
    FavoritesSnapshot,
    FavoritesSyncProgress,
    SearchResultSnapshot,
)
from ..chapter_download_flow import ChapterDownloadFlow
from ..controllers.account_controller import AccountController
from ..icons import arrow_icon, svg_icon
from ..widgets.search_cover_loader import SearchCoverLoader
from ..widgets.favorite_folder_dialogs import (
    FavoriteFolderManagerDialog,
    FavoriteTargetDialog,
)
from ..widgets.search_result_card import SearchResultCard
from .base import SectionPage

if TYPE_CHECKING:
    from ..controllers.download_controller import DownloadController
    from ..controllers.chapter_catalog_controller import ChapterCatalogController
    from ..controllers.favorites_controller import FavoritesController


FAVORITES_PAGE_SIZE = 20


class FavoritesPage(SectionPage):
    view_task_requested = Signal(str)

    def __init__(
        self,
        controller: AccountController | None = None,
        parent=None,
        *,
        favorites_controller: "FavoritesController | None" = None,
        download_controller: "DownloadController | None" = None,
        cover_service=None,
        cover_loader: SearchCoverLoader | None = None,
        chapter_catalog_controller: "ChapterCatalogController | None" = None,
    ):
        super().__init__("我的收藏", "favoritesPage", parent)
        self.controller = controller
        self.favorites_controller = favorites_controller
        self.download_controller = download_controller
        self._snapshot = (
            controller.current_snapshot
            if controller is not None
            else AccountSnapshot(AccountStatus.SIGNED_OUT)
        )
        self._busy = bool(controller is not None and controller.is_busy)
        self._favorites_snapshot = (
            favorites_controller.current_snapshot
            if favorites_controller is not None
            else None
        )
        self._favorites_busy = bool(
            favorites_controller is not None and favorites_controller.is_busy
        )
        self._favorites_command = (
            favorites_controller.current_command
            if favorites_controller is not None
            else ""
        )
        self._favorites_error_code = ""
        self._selected_folder_id: str | None = None
        self._pending_order_by = (
            self._favorites_snapshot.order_by
            if self._favorites_snapshot is not None
            else "mr"
        )
        self._filter_generation: int | None = None
        self._filtered_snapshot: FavoritesFilterSnapshot | None = None
        self._local_page = 1
        self._tasks_by_album = set()
        self._cards_by_album: dict[str, list[SearchResultCard]] = {}
        self._chapter_catalogs: dict[str, ChapterCatalogSnapshot] = {}
        self._cover_generation = 0
        self._cover_attempted: set[tuple[int, str]] = set()
        self._cover_update_scheduled = False
        self._column_count = 0
        self._disposed = False
        self.favorite_cards: tuple[SearchResultCard, ...] = ()
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(160)
        self._filter_timer.timeout.connect(self._submit_filter)

        self._owns_cover_loader = False
        if cover_loader is not None:
            self._cover_loader = cover_loader
        elif cover_service is not None and callable(
            getattr(cover_service, "fetch_cover", None)
        ):
            self._cover_loader = SearchCoverLoader(cover_service, self)
            self._owns_cover_loader = True
        else:
            self._cover_loader = None
        if self._cover_loader is not None:
            self._cover_loader.cover_ready.connect(self._on_cover_ready)
            self._cover_loader.cover_failed.connect(self._on_cover_failed)

        self.state_stack = QStackedWidget(self.content)
        self.state_stack.setObjectName("accountStateStack")
        self.loading_state = self._create_loading_state()
        self.login_state = self._create_login_state()
        self.account_state = self._create_account_state()
        for widget in (
            self.loading_state,
            self.login_state,
            self.account_state,
        ):
            self.state_stack.addWidget(widget)
        self.content_layout.addWidget(self.state_stack, 1)

        if controller is not None:
            controller.snapshot_changed.connect(self._on_snapshot)
            controller.busy_changed.connect(self._on_busy_changed)
            controller.operation_failed.connect(self._on_operation_failed)
        if favorites_controller is not None:
            favorites_controller.snapshot_changed.connect(
                self._on_favorites_snapshot
            )
            favorites_controller.progress_changed.connect(
                self._on_sync_progress
            )
            favorites_controller.operation_failed.connect(
                self._on_favorites_failed
            )
            favorites_controller.busy_changed.connect(
                self._on_favorites_busy_changed
            )
            favorites_controller.filter_result_changed.connect(
                self._on_filter_result
            )
            favorites_controller.mutation_succeeded.connect(
                self._on_mutation_succeeded
            )
            favorites_controller.mutation_failed.connect(
                self._on_mutation_failed
            )
            favorites_controller.mutation_refresh_failed.connect(
                self._on_mutation_refresh_failed
            )
        if download_controller is not None:
            download_controller.tasks_reset.connect(self._set_tasks)
            self._set_tasks(download_controller.list_tasks())
        self._chapter_flow = ChapterDownloadFlow(
            download_controller,
            chapter_catalog_controller,
            self,
        )
        self._chapter_flow.loading_changed.connect(
            self._on_chapter_loading_changed
        )
        self._chapter_flow.catalog_resolved.connect(
            self._on_chapter_catalog_resolved
        )
        self._chapter_flow.task_created.connect(
            self._on_chapter_task_created
        )
        self._chapter_flow.failed.connect(self._on_chapter_flow_failed)
        if self._favorites_snapshot is not None:
            self._update_sort_selection(self._favorites_snapshot.order_by)
            self._rebuild_folder_options()
        self._render()
        QTimer.singleShot(0, self._reflow_cards)

    def _create_loading_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountLoadingState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(0, 72, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        title = QLabel("正在读取本地登录信息", state)
        title.setObjectName("accountStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        detail = QLabel("此过程不会连接网络", state)
        detail.setObjectName("accountStateDetail")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)
        return state

    def _create_login_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountLoginState")
        outer = QVBoxLayout(state)
        outer.setContentsMargins(0, 42, 0, 0)
        outer.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        form = QWidget(state)
        form.setObjectName("accountLoginForm")
        form.setMaximumWidth(460)
        form.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.login_title = QLabel("登录账号", form)
        self.login_title.setObjectName("accountStateTitle")
        layout.addWidget(self.login_title)
        self.login_detail = QLabel("登录信息将加密保存在程序目录", form)
        self.login_detail.setObjectName("accountStateDetail")
        self.login_detail.setWordWrap(True)
        layout.addWidget(self.login_detail)

        self.username_input = QLineEdit(form)
        self.username_input.setObjectName("accountUsernameInput")
        self.username_input.setPlaceholderText("账号")
        self.username_input.setClearButtonEnabled(True)
        self.username_input.setMaxLength(128)
        self.username_input.setFixedHeight(40)
        layout.addWidget(self.username_input)

        self.password_input = QLineEdit(form)
        self.password_input.setObjectName("accountPasswordInput")
        self.password_input.setPlaceholderText("密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMaxLength(512)
        self.password_input.setFixedHeight(40)
        self.password_input.returnPressed.connect(self._submit_login)
        layout.addWidget(self.password_input)

        self.login_error = QLabel("", form)
        self.login_error.setObjectName("accountErrorLabel")
        self.login_error.setWordWrap(True)
        self.login_error.hide()
        layout.addWidget(self.login_error)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 4, 0, 0)
        actions.setSpacing(8)
        self.clear_button = QPushButton("清除本地账号数据", form)
        self.clear_button.setObjectName("accountSecondaryButton")
        self.clear_button.clicked.connect(self._confirm_clear_local_data)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        self.login_button = QPushButton("登录", form)
        self.login_button.setObjectName("accountPrimaryButton")
        self.login_button.setIcon(svg_icon("user-check"))
        self.login_button.setFixedHeight(38)
        self.login_button.clicked.connect(self._submit_login)
        actions.addWidget(self.login_button)
        layout.addLayout(actions)
        outer.addWidget(form)
        return state

    def _create_account_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountSignedInState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)

        summary = QFrame(state)
        summary.setObjectName("accountSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(16, 12, 12, 12)
        summary_layout.setSpacing(10)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        self.account_name = QLabel("", summary)
        self.account_name.setObjectName("accountName")
        self.account_name.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        text_layout.addWidget(self.account_name)
        self.account_status = QLabel("", summary)
        self.account_status.setObjectName("accountStateDetail")
        text_layout.addWidget(self.account_status)
        self.last_sync_label = QLabel("尚未同步", summary)
        self.last_sync_label.setObjectName("favoritesLastSync")
        text_layout.addWidget(self.last_sync_label)
        summary_layout.addLayout(text_layout, 1)

        self.sync_button = QToolButton(summary)
        self.sync_button.setObjectName("favoritesSyncButton")
        self.sync_button.setText("同步")
        self.sync_button.setIcon(svg_icon("refresh"))
        self.sync_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.sync_button.setFixedSize(88, 36)
        self.sync_button.clicked.connect(self._sync_or_stop)
        summary_layout.addWidget(self.sync_button)

        self.logout_button = QToolButton(summary)
        self.logout_button.setObjectName("accountLogoutButton")
        self.logout_button.setText("退出登录")
        self.logout_button.setIcon(svg_icon("user-delete"))
        self.logout_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.logout_button.setFixedSize(104, 36)
        self.logout_button.clicked.connect(self._confirm_logout)
        summary_layout.addWidget(self.logout_button)
        layout.addWidget(summary)

        self.expired_panel = QFrame(state)
        self.expired_panel.setObjectName("favoritesExpiredPanel")
        expired_layout = QHBoxLayout(self.expired_panel)
        expired_layout.setContentsMargins(12, 8, 10, 8)
        expired_layout.setSpacing(8)
        expired_text = QLabel("登录已过期，仍可查看本地收藏", self.expired_panel)
        expired_text.setObjectName("favoritesBannerText")
        expired_layout.addWidget(expired_text, 1)
        self.expired_password_input = QLineEdit(self.expired_panel)
        self.expired_password_input.setObjectName("favoritesReloginPassword")
        self.expired_password_input.setPlaceholderText("输入密码重新登录")
        self.expired_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.expired_password_input.setMaxLength(512)
        self.expired_password_input.setFixedSize(210, 34)
        self.expired_password_input.returnPressed.connect(self._submit_relogin)
        expired_layout.addWidget(self.expired_password_input)
        self.relogin_button = QPushButton("重新登录", self.expired_panel)
        self.relogin_button.setObjectName("accountPrimaryButton")
        self.relogin_button.setFixedSize(92, 34)
        self.relogin_button.clicked.connect(self._submit_relogin)
        expired_layout.addWidget(self.relogin_button)
        layout.addWidget(self.expired_panel)

        self.expired_error = QLabel("", state)
        self.expired_error.setObjectName("accountErrorLabel")
        self.expired_error.setWordWrap(True)
        self.expired_error.hide()
        layout.addWidget(self.expired_error)

        self.sync_progress_frame = QFrame(state)
        self.sync_progress_frame.setObjectName("favoritesProgressFrame")
        progress_layout = QHBoxLayout(self.sync_progress_frame)
        progress_layout.setContentsMargins(10, 6, 10, 6)
        progress_layout.setSpacing(10)
        self.sync_progress_label = QLabel("正在同步收藏", self.sync_progress_frame)
        self.sync_progress_label.setObjectName("favoritesProgressLabel")
        progress_layout.addWidget(self.sync_progress_label, 1)
        self.sync_progress_bar = QProgressBar(self.sync_progress_frame)
        self.sync_progress_bar.setObjectName("favoritesProgressBar")
        self.sync_progress_bar.setTextVisible(False)
        self.sync_progress_bar.setFixedSize(180, 6)
        progress_layout.addWidget(self.sync_progress_bar)
        self.sync_progress_frame.hide()
        layout.addWidget(self.sync_progress_frame)

        self.favorites_error_banner = QFrame(state)
        self.favorites_error_banner.setObjectName("favoritesErrorBanner")
        error_layout = QHBoxLayout(self.favorites_error_banner)
        error_layout.setContentsMargins(10, 7, 10, 7)
        self.favorites_error_label = QLabel("", self.favorites_error_banner)
        self.favorites_error_label.setObjectName("favoritesBannerText")
        self.favorites_error_label.setWordWrap(True)
        error_layout.addWidget(self.favorites_error_label, 1)
        self.favorites_error_banner.hide()
        layout.addWidget(self.favorites_error_banner)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.folder_button = QToolButton(state)
        self.folder_button.setObjectName("favoritesFolderButton")
        self.folder_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.folder_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.folder_button.setMinimumWidth(220)
        self.folder_button.setMaximumWidth(360)
        self.folder_button.setFixedHeight(36)
        self.folder_menu = QMenu(self.folder_button)
        self.folder_menu.setObjectName("favoritesFolderMenu")
        self.folder_menu.triggered.connect(self._select_folder)
        self.folder_button.setMenu(self.folder_menu)
        filter_row.addWidget(self.folder_button)
        self.sort_button = QToolButton(state)
        self.sort_button.setObjectName("favoritesSortButton")
        self.sort_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.sort_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.sort_button.setFixedSize(130, 36)
        self.sort_menu = QMenu(self.sort_button)
        self.sort_menu.setObjectName("favoritesSortMenu")
        for label, order_by in (("收藏时间", "mr"), ("更新时间", "mp")):
            action = self.sort_menu.addAction(label)
            action.setData(order_by)
            action.setCheckable(True)
        self.sort_menu.triggered.connect(self._select_sort)
        self.sort_button.setMenu(self.sort_menu)
        self._update_sort_selection(self._pending_order_by)
        filter_row.addWidget(self.sort_button)
        self.manage_folders_button = QToolButton(state)
        self.manage_folders_button.setObjectName("favoritesManageButton")
        self.manage_folders_button.setIcon(svg_icon("folder"))
        self.manage_folders_button.setToolTip("管理收藏夹")
        self.manage_folders_button.setFixedSize(36, 36)
        self.manage_folders_button.setAttribute(
            Qt.WidgetAttribute.WA_Hover,
            True,
        )
        self.manage_folders_button.setMouseTracking(True)
        self.manage_folders_button.clicked.connect(self._open_folder_manager)
        filter_row.addWidget(self.manage_folders_button)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        self.keyword_input = QLineEdit(state)
        self.keyword_input.setObjectName("favoritesKeywordInput")
        self.keyword_input.setPlaceholderText("筛选标题、作者或 JM 号")
        self.keyword_input.setClearButtonEnabled(True)
        self.keyword_input.setMaximumWidth(420)
        self.keyword_input.setFixedHeight(36)
        self.keyword_input.textChanged.connect(self._queue_filter)
        search_row.addWidget(self.keyword_input, 1)
        self.favorites_summary = QLabel("", state)
        self.favorites_summary.setObjectName("favoritesResultsSummary")
        search_row.addWidget(self.favorites_summary)
        layout.addLayout(search_row)

        self.favorites_stack = QStackedWidget(state)
        self.favorites_stack.setObjectName("favoritesContentStack")
        self.favorite_idle_state = self._create_favorite_state(
            "favoritesIdleState",
            "尚未同步收藏",
        )
        self.favorite_loading_state = self._create_favorite_state(
            "favoritesLoadingState",
            "正在读取本地收藏",
            loading=True,
        )
        self.favorite_empty_state = self._create_favorite_state(
            "favoritesEmptyState",
            "当前文件夹没有收藏",
        )
        self.favorite_empty_label = self.favorite_empty_state.findChild(
            QLabel,
            "favoritesStateLabel",
        )
        self.favorite_error_state = self._create_favorite_state(
            "favoritesDataErrorState",
            "本地收藏暂时无法显示",
        )
        self.favorite_results_state = self._create_results_state()
        for widget in (
            self.favorite_idle_state,
            self.favorite_loading_state,
            self.favorite_empty_state,
            self.favorite_error_state,
            self.favorite_results_state,
        ):
            self.favorites_stack.addWidget(widget)
        layout.addWidget(self.favorites_stack, 1)
        return state

    def _create_favorite_state(
        self,
        object_name: str,
        message: str,
        *,
        loading: bool = False,
    ) -> QWidget:
        state = QWidget(self)
        state.setObjectName(object_name)
        layout = QVBoxLayout(state)
        layout.addStretch(1)
        label = QLabel(message, state)
        label.setObjectName("favoritesStateLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        if loading:
            progress = QProgressBar(state)
            progress.setRange(0, 0)
            progress.setTextVisible(False)
            progress.setFixedSize(180, 4)
            layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return state

    def _create_results_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("favoritesResultsState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.results_scroll = QScrollArea(state)
        self.results_scroll.setObjectName("favoritesResultsScroll")
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.results_canvas = QWidget(self.results_scroll)
        self.results_canvas.setObjectName("favoritesResultsCanvas")
        self.results_grid = QGridLayout(self.results_canvas)
        self.results_grid.setContentsMargins(0, 0, 8, 0)
        self.results_grid.setHorizontalSpacing(16)
        self.results_grid.setVerticalSpacing(16)
        self.results_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.results_scroll.setWidget(self.results_canvas)
        self.results_scroll.viewport().installEventFilter(self)
        self.results_scroll.verticalScrollBar().valueChanged.connect(
            self._schedule_visible_covers
        )
        layout.addWidget(self.results_scroll, 1)

        self.pagination = QFrame(state)
        self.pagination.setObjectName("favoritesPagination")
        self.pagination.setFixedHeight(38)
        pagination_layout = QHBoxLayout(self.pagination)
        pagination_layout.setContentsMargins(0, 2, 0, 2)
        pagination_layout.setSpacing(12)
        pagination_layout.addStretch(1)
        self.previous_page_button = QToolButton(self.pagination)
        self.previous_page_button.setObjectName("favoritesPageButton")
        self.previous_page_button.setIcon(arrow_icon("left"))
        self.previous_page_button.setToolTip("上一页")
        self.previous_page_button.setFixedSize(32, 32)
        self.previous_page_button.clicked.connect(self._previous_page)
        pagination_layout.addWidget(self.previous_page_button)
        self.page_label = QLabel("第 1 / 1 页", self.pagination)
        self.page_label.setObjectName("favoritesPageLabel")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(96)
        pagination_layout.addWidget(self.page_label)
        self.next_page_button = QToolButton(self.pagination)
        self.next_page_button.setObjectName("favoritesPageButton")
        self.next_page_button.setIcon(arrow_icon("right"))
        self.next_page_button.setToolTip("下一页")
        self.next_page_button.setFixedSize(32, 32)
        self.next_page_button.clicked.connect(self._next_page)
        pagination_layout.addWidget(self.next_page_button)
        pagination_layout.addStretch(1)
        layout.addWidget(self.pagination)
        return state

    def activate(self) -> None:
        self._schedule_visible_covers()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._filter_timer.stop()
        self._chapter_flow.dispose()
        if self._owns_cover_loader and self._cover_loader is not None:
            self._cover_loader.dispose()

    def eventFilter(self, watched, event):
        if (
            watched is self.results_scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._reflow_cards)
        return super().eventFilter(watched, event)

    @Slot()
    def _submit_login(self) -> None:
        password = self.password_input.text()
        self.password_input.clear()
        self._set_login_error("")
        if self.controller is None:
            self._set_login_error("账号服务未初始化")
            return
        self.controller.login(self.username_input.text(), password)
        password = ""

    @Slot()
    def _submit_relogin(self) -> None:
        password = self.expired_password_input.text()
        self.expired_password_input.clear()
        self._set_expired_error("")
        if self.controller is None:
            self._set_expired_error("账号服务未初始化")
            return
        self.controller.login(self._snapshot.username or "", password)
        password = ""

    @Slot()
    def _sync_or_stop(self) -> None:
        if self.favorites_controller is None:
            self._set_favorites_error("收藏同步服务未初始化")
            return
        if self._favorites_busy and self._favorites_command == "sync":
            self.favorites_controller.cancel_sync()
        elif not self._favorites_busy:
            self._set_favorites_error("")
            self._favorites_error_code = ""
            self.favorites_controller.sync(self._pending_order_by)

    @Slot()
    def _confirm_logout(self) -> None:
        if self.controller is None:
            return
        answer = QMessageBox.question(
            self,
            "退出登录",
            "退出后将删除程序目录中的本地账号信息和收藏缓存，确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.logout()

    @Slot()
    def _confirm_clear_local_data(self) -> None:
        if self.controller is None:
            return
        answer = QMessageBox.question(
            self,
            "清除本地账号数据",
            "将删除 account.dat 和 favorites.dat，原文件不会自动备份。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.logout()

    @Slot(object)
    def _on_snapshot(self, snapshot: AccountSnapshot) -> None:
        if not isinstance(snapshot, AccountSnapshot):
            return
        self._snapshot = snapshot
        self._set_login_error("")
        self._set_expired_error("")
        self._render()

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._render_controls()

    @Slot(str, str)
    def _on_operation_failed(self, _code: str, message: str) -> None:
        if self._snapshot.status is AccountStatus.EXPIRED:
            self._set_expired_error(message)
        else:
            self._set_login_error(message)

    @Slot(object)
    def _on_favorites_snapshot(self, snapshot) -> None:
        if snapshot is not None and not isinstance(snapshot, FavoritesSnapshot):
            return
        self._favorites_snapshot = snapshot
        if snapshot is not None:
            self._update_sort_selection(snapshot.order_by)
        self._filtered_snapshot = None
        self._filter_generation = None
        self._favorites_error_code = ""
        self._set_favorites_error("")
        self._rebuild_folder_options()
        self._render_controls()
        self._render_favorites()

    @Slot(object)
    def _on_sync_progress(self, progress) -> None:
        if progress is not None and not isinstance(
            progress,
            FavoritesSyncProgress,
        ):
            return
        if progress is None:
            self.sync_progress_bar.setRange(0, 0)
            self.sync_progress_label.setText("正在同步收藏")
            return
        self.sync_progress_label.setText(
            f"{progress.folder_name} · 第 {progress.page} / "
            f"{max(1, progress.page_count)} 页 · "
            f"{progress.received_items} / {progress.expected_items} 条"
        )
        if progress.folder_count > 0 and progress.page_count > 0:
            completed = (
                progress.folder_index - 1
                + progress.page / progress.page_count
            )
            percent = round(100 * completed / progress.folder_count)
            self.sync_progress_bar.setRange(0, 100)
            self.sync_progress_bar.setValue(max(0, min(100, percent)))

    @Slot(str, str)
    def _on_favorites_failed(self, code: str, message: str) -> None:
        self._favorites_error_code = code
        self._set_favorites_error(message)
        self._render_favorites()

    @Slot(bool, str)
    def _on_favorites_busy_changed(self, busy: bool, command: str) -> None:
        self._favorites_busy = bool(busy)
        self._favorites_command = command if busy else ""
        self._render_controls()
        self._render_favorites()

    @Slot(int, object)
    def _on_filter_result(self, generation: int, snapshot) -> None:
        if (
            not isinstance(snapshot, FavoritesFilterSnapshot)
            or generation != self._filter_generation
        ):
            return
        self._filtered_snapshot = snapshot
        self._render_favorites()

    @Slot(str, str)
    def _on_mutation_succeeded(self, command: str, _value: str) -> None:
        if command == "move_album":
            self._set_favorites_feedback("已移动并刷新收藏", error=False)

    @Slot(str, str, str)
    def _on_mutation_failed(
        self,
        _command: str,
        code: str,
        message: str,
    ) -> None:
        self._favorites_error_code = code
        self._set_favorites_feedback(message, error=True)

    @Slot(str, str, str)
    def _on_mutation_refresh_failed(
        self,
        _command: str,
        code: str,
        message: str,
    ) -> None:
        self._favorites_error_code = code
        self._set_favorites_feedback(message, error=True)

    def _render(self) -> None:
        status = self._snapshot.status
        if status is AccountStatus.RESTORING:
            self.state_stack.setCurrentWidget(self.loading_state)
        elif status in {
            AccountStatus.SAVED_SESSION,
            AccountStatus.SIGNED_IN,
            AccountStatus.EXPIRED,
        }:
            self.account_name.setText(self._snapshot.username or "已登录账号")
            self.account_name.setToolTip(self.account_name.text())
            if status is AccountStatus.SAVED_SESSION:
                self.account_status.setText("本地会话已恢复，尚未联网验证")
            elif status is AccountStatus.EXPIRED:
                self.account_status.setText("会话已过期，本地缓存仍可查看")
            else:
                self.account_status.setText("本次会话已验证")
            self.expired_panel.setVisible(status is AccountStatus.EXPIRED)
            self.expired_error.setVisible(
                status is AccountStatus.EXPIRED
                and bool(self.expired_error.text())
            )
            self.state_stack.setCurrentWidget(self.account_state)
            self._render_favorites()
        else:
            if status is AccountStatus.LOCAL_DATA_UNREADABLE:
                self.login_title.setText("本地登录信息无法读取")
                self.login_detail.setText("可以重新登录覆盖原文件，或清除本地数据")
                self.login_button.setText("重新登录")
            elif status is AccountStatus.SIGNING_IN:
                self.login_title.setText("正在登录")
                self.login_detail.setText("正在验证账号信息")
                self.login_button.setText("登录中")
            else:
                self.login_title.setText("登录账号")
                self.login_detail.setText("登录信息将加密保存在程序目录")
                self.login_button.setText("登录")
            self.clear_button.setVisible(
                status is AccountStatus.LOCAL_DATA_UNREADABLE
            )
            self.state_stack.setCurrentWidget(self.login_state)
        self._render_controls()

    def _render_controls(self) -> None:
        add_in_progress = (
            self._favorites_busy and self._favorites_command == "add"
        )
        account_enabled = (
            self.controller is not None
            and not self._busy
            and not add_in_progress
        )
        self.username_input.setEnabled(account_enabled)
        self.password_input.setEnabled(account_enabled)
        self.login_button.setEnabled(account_enabled)
        self.clear_button.setEnabled(account_enabled)
        self.logout_button.setEnabled(account_enabled)
        self.expired_password_input.setEnabled(account_enabled)
        self.relogin_button.setEnabled(account_enabled)

        status_allows_sync = self._snapshot.status in {
            AccountStatus.SAVED_SESSION,
            AccountStatus.SIGNED_IN,
        }
        if self._favorites_busy and self._favorites_command == "sync":
            self.sync_button.setText("停止")
            self.sync_button.setIcon(svg_icon("stop"))
            self.sync_button.setToolTip("停止本次同步")
            self.sync_button.setEnabled(True)
        else:
            self.sync_button.setText("同步")
            self.sync_button.setIcon(svg_icon("refresh"))
            self.sync_button.setToolTip("从远端完整同步收藏")
            self.sync_button.setEnabled(
                self.favorites_controller is not None
                and not self._busy
                and not self._favorites_busy
                and status_allows_sync
            )
        self.sync_progress_frame.setVisible(
            self._favorites_busy and self._favorites_command == "sync"
        )
        self.folder_button.setEnabled(
            bool(self.folder_menu.actions()) and not self._favorites_busy
        )
        if self._favorites_busy and self._favorites_command == "restore":
            self.folder_button.setToolTip("正在恢复本地收藏")
        elif self._favorites_busy:
            self.folder_button.setToolTip("收藏操作完成后可切换文件夹")
        else:
            self.folder_button.setToolTip("切换当前收藏夹")
        self.sort_button.setEnabled(not self._favorites_busy)
        full_snapshot = (
            self._favorites_snapshot is not None
            and self._favorites_snapshot.synced_at_utc is not None
        )
        remote_available = self._snapshot.status in {
            AccountStatus.SAVED_SESSION,
            AccountStatus.SIGNED_IN,
        }
        self.manage_folders_button.setEnabled(
            self.favorites_controller is not None
            and full_snapshot
            and remote_available
            and not self._favorites_busy
        )
        self.keyword_input.setEnabled(full_snapshot)
        for card in self.favorite_cards:
            card.action_button.setEnabled(self.download_controller is not None)
            card.set_move_favorite_available(
                remote_available and full_snapshot and not self._favorites_busy
            )

    def _render_favorites(self) -> None:
        snapshot = self._favorites_snapshot
        self.last_sync_label.setText(
            "尚未同步"
            if snapshot is None or snapshot.synced_at_utc is None
            else f"最后同步：{_display_timestamp(snapshot.synced_at_utc)}"
        )
        if (
            snapshot is not None
            and snapshot.synced_at_utc is not None
            and self._pending_order_by != snapshot.order_by
        ):
            pending_name = (
                "收藏时间" if self._pending_order_by == "mr" else "更新时间"
            )
            self.last_sync_label.setText(
                f"{self.last_sync_label.text()} · 待按{pending_name}同步"
            )
        if (
            self._favorites_busy
            and self._favorites_command == "restore"
            and snapshot is None
        ):
            self.favorites_stack.setCurrentWidget(self.favorite_loading_state)
            self.favorites_summary.clear()
            return
        if snapshot is None and self._favorites_error_code in {
            "local_data_unreadable",
            "account_mismatch",
        }:
            self._set_cards(())
            self.favorites_stack.setCurrentWidget(self.favorite_error_state)
            self.favorites_summary.clear()
            return
        if snapshot is None or snapshot.synced_at_utc is None:
            self._set_cards(())
            self.favorites_stack.setCurrentWidget(self.favorite_idle_state)
            self.favorites_summary.clear()
            return
        folder = self._current_folder()
        if folder is None:
            self._set_cards(())
            self.favorites_stack.setCurrentWidget(self.favorite_error_state)
            self.favorites_summary.clear()
            return
        keyword = " ".join(self.keyword_input.text().split()).casefold()
        if keyword:
            filtered = self._filtered_snapshot
            if (
                filtered is None
                or filtered.folder_id != folder.folder_id
                or filtered.keyword != keyword
            ):
                self._set_cards(())
                self.favorites_summary.setText("正在筛选…")
                self.favorites_stack.setCurrentWidget(
                    self.favorite_loading_state
                )
                return
            items = filtered.items
        else:
            items = folder.items
        total = len(items)
        page_count = max(1, (total + FAVORITES_PAGE_SIZE - 1) // FAVORITES_PAGE_SIZE)
        self._local_page = max(1, min(self._local_page, page_count))
        self.favorites_summary.setText(
            f"共 {total} 条"
            + (f" · 第 {self._local_page} / {page_count} 页" if page_count > 1 else "")
        )
        if total == 0:
            self._set_cards(())
            self.pagination.hide()
            if self.favorite_empty_label is not None:
                self.favorite_empty_label.setText(
                    "没有匹配的收藏"
                    if keyword
                    else "当前文件夹没有收藏"
                )
            self.favorites_stack.setCurrentWidget(self.favorite_empty_state)
            return
        start = (self._local_page - 1) * FAVORITES_PAGE_SIZE
        self._set_cards(items[start : start + FAVORITES_PAGE_SIZE])
        self.page_label.setText(f"第 {self._local_page} / {page_count} 页")
        self.previous_page_button.setEnabled(self._local_page > 1)
        self.next_page_button.setEnabled(self._local_page < page_count)
        self.pagination.setVisible(page_count > 1)
        self.favorites_stack.setCurrentWidget(self.favorite_results_state)
        self.results_scroll.verticalScrollBar().setValue(0)
        self._schedule_visible_covers()

    def _rebuild_folder_options(self) -> None:
        snapshot = self._favorites_snapshot
        folders = snapshot.folders if snapshot is not None else ()
        selected = self._selected_folder_id
        folder_ids = {folder.folder_id for folder in folders}
        if selected not in folder_ids:
            selected = folders[0].folder_id if folders else None
        self.folder_menu.clear()
        for folder in folders:
            action = self.folder_menu.addAction(
                f"{folder.name} ({len(folder.items)})"
            )
            action.setData(folder.folder_id)
            action.setCheckable(True)
            action.setChecked(folder.folder_id == selected)
        self._selected_folder_id = selected
        self._update_folder_button_text()
        self._local_page = 1

    def _current_folder(self) -> FavoriteFolderSnapshot | None:
        snapshot = self._favorites_snapshot
        if snapshot is None:
            return None
        for folder in snapshot.folders:
            if folder.folder_id == self._selected_folder_id:
                return folder
        return snapshot.folders[0] if snapshot.folders else None

    @Slot(QAction)
    def _select_folder(self, action: QAction) -> None:
        folder_id = action.data()
        snapshot = self._favorites_snapshot
        if not isinstance(folder_id, str) or snapshot is None:
            return
        if not any(folder.folder_id == folder_id for folder in snapshot.folders):
            return
        self._selected_folder_id = folder_id
        for candidate in self.folder_menu.actions():
            candidate.setChecked(candidate is action)
        self._update_folder_button_text()
        self._local_page = 1
        self._filtered_snapshot = None
        self._queue_filter()
        if self.keyword_input.text().strip():
            self._filter_timer.stop()
            self._submit_filter()

    def _update_folder_button_text(self) -> None:
        current = next(
            (
                action
                for action in self.folder_menu.actions()
                if action.data() == self._selected_folder_id
            ),
            None,
        )
        self.folder_button.setText(
            "选择收藏夹 ▾" if current is None else f"{current.text()}  ▾"
        )

    @Slot(QAction)
    def _select_sort(self, action: QAction) -> None:
        order_by = action.data()
        if order_by not in {"mr", "mp"}:
            return
        self._update_sort_selection(order_by)
        self._render_controls()
        self._render_favorites()

    def _update_sort_selection(self, order_by: str) -> None:
        if order_by not in {"mr", "mp"}:
            return
        self._pending_order_by = order_by
        current = None
        for action in self.sort_menu.actions():
            selected = action.data() == order_by
            action.setChecked(selected)
            if selected:
                current = action
        if current is not None:
            self.sort_button.setText(f"{current.text()}  ▾")

    @Slot()
    def _queue_filter(self, *_args) -> None:
        self._local_page = 1
        self._filtered_snapshot = None
        keyword = " ".join(self.keyword_input.text().split())
        if not keyword:
            self._filter_timer.stop()
            self._filter_generation = None
            self._render_favorites()
            return
        self._filter_timer.start()
        self._render_favorites()

    @Slot()
    def _submit_filter(self) -> None:
        if self.favorites_controller is None:
            return
        folder = self._current_folder()
        if folder is None:
            return
        self._filter_generation = self.favorites_controller.filter_items(
            folder.folder_id,
            self.keyword_input.text(),
        )

    @Slot()
    def _open_folder_manager(self) -> None:
        if (
            self.favorites_controller is None
            or not self.manage_folders_button.isEnabled()
        ):
            return
        FavoriteFolderManagerDialog(
            self.favorites_controller,
            self,
        ).exec()

    @Slot(str)
    def _move_favorite(self, album_id: str) -> None:
        if self.favorites_controller is None or self._favorites_busy:
            return
        snapshot = self._favorites_snapshot
        if snapshot is None or snapshot.synced_at_utc is None:
            return
        current_id = self._selected_folder_id
        folders = tuple(
            folder
            for folder in snapshot.folders
            if folder.folder_id != current_id or current_id == "0"
        )
        folder_id = FavoriteTargetDialog.choose(
            folders,
            self,
            title="移动收藏",
            description="选择这本漫画的新位置。移动后会自动刷新收藏。",
        )
        if folder_id is None:
            return
        self._set_favorites_error("")
        self.favorites_controller.move_album(album_id, folder_id)

    def _previous_page(self) -> None:
        if self._local_page > 1:
            self._local_page -= 1
            self._render_favorites()

    def _next_page(self) -> None:
        folder = self._current_folder()
        if folder is None:
            return
        items = folder.items
        keyword = " ".join(self.keyword_input.text().split()).casefold()
        if (
            keyword
            and self._filtered_snapshot is not None
            and self._filtered_snapshot.folder_id == folder.folder_id
            and self._filtered_snapshot.keyword == keyword
        ):
            items = self._filtered_snapshot.items
        page_count = max(
            1,
            (len(items) + FAVORITES_PAGE_SIZE - 1)
            // FAVORITES_PAGE_SIZE,
        )
        if self._local_page < page_count:
            self._local_page += 1
            self._render_favorites()

    def _set_cards(self, items) -> None:
        while self.results_grid.count():
            self.results_grid.takeAt(0)
        for card in self.favorite_cards:
            card.hide()
            card.deleteLater()
        self._cover_generation += 1
        self._cover_attempted.clear()
        self._cards_by_album = {}
        cards = []
        for item in items:
            card = SearchResultCard(
                SearchResultSnapshot(
                    item.album_id,
                    item.title,
                    item.authors,
                    item.tags,
                ),
                self.results_canvas,
            )
            card.set_task_present(item.album_id in self._tasks_by_album)
            cached_catalog = self._chapter_catalogs.get(item.album_id)
            if cached_catalog is not None:
                card.set_chapter_state(cached_catalog)
            card.set_action_available(self.download_controller is not None)
            card.download_requested.connect(self._download_favorite)
            card.view_task_requested.connect(self.view_task_requested)
            card.set_move_favorite_visible(True)
            card.set_move_favorite_available(
                self._snapshot.status
                in {AccountStatus.SAVED_SESSION, AccountStatus.SIGNED_IN}
                and self._favorites_snapshot is not None
                and self._favorites_snapshot.synced_at_utc is not None
                and not self._favorites_busy
            )
            card.move_favorite_requested.connect(self._move_favorite)
            cards.append(card)
            self._cards_by_album.setdefault(item.album_id, []).append(card)
        self.favorite_cards = tuple(cards)
        self._column_count = 0
        self._reflow_cards()

    def _reflow_cards(self) -> None:
        if not hasattr(self, "results_scroll"):
            return
        available_width = max(1, self.results_scroll.viewport().width() - 8)
        spacing = self.results_grid.horizontalSpacing()
        step = SearchResultCard.WIDTH + spacing
        columns = max(1, (available_width + spacing) // step)
        if (
            columns == self._column_count
            and self.results_grid.count() == len(self.favorite_cards)
        ):
            self._schedule_visible_covers()
            return
        while self.results_grid.count():
            self.results_grid.takeAt(0)
        for index, card in enumerate(self.favorite_cards):
            self.results_grid.addWidget(card, index // columns, index % columns)
        self._column_count = columns
        self._schedule_visible_covers()

    def _schedule_visible_covers(self, *_args) -> None:
        if self._disposed or self._cover_update_scheduled:
            return
        self._cover_update_scheduled = True
        QTimer.singleShot(0, self._request_visible_covers)

    def _request_visible_covers(self) -> None:
        self._cover_update_scheduled = False
        if (
            self._disposed
            or self._cover_loader is None
            or not self.favorite_cards
            or self.favorites_stack.currentWidget() is not self.favorite_results_state
        ):
            return
        columns = max(1, self._column_count)
        row_step = SearchResultCard.HEIGHT + self.results_grid.verticalSpacing()
        scroll_top = self.results_scroll.verticalScrollBar().value()
        viewport_height = max(1, self.results_scroll.viewport().height())
        first_row = max(0, scroll_top // row_step)
        last_visible_row = max(
            first_row,
            (scroll_top + viewport_height - 1) // row_step,
        )
        final_row = min(
            (len(self.favorite_cards) - 1) // columns,
            last_visible_row + 1,
        )
        start = first_row * columns
        stop = min(len(self.favorite_cards), (final_row + 1) * columns)
        target_size = QSize(SearchResultCard.WIDTH - 2, SearchResultCard.COVER_HEIGHT)
        for card in self.favorite_cards[start:stop]:
            key = (self._cover_generation, card.snapshot.album_id)
            if key in self._cover_attempted:
                continue
            if self._cover_loader.request(
                self._cover_generation,
                card.snapshot.album_id,
                target_size,
            ):
                self._cover_attempted.add(key)

    @Slot(int, str, object)
    def _on_cover_ready(self, generation: int, album_id: str, image) -> None:
        if generation != self._cover_generation or self._disposed:
            return
        for card in self._cards_by_album.get(album_id, ()):
            card.set_cover(image)

    @Slot(int, str)
    def _on_cover_failed(self, generation: int, album_id: str) -> None:
        if generation != self._cover_generation or self._disposed:
            return
        for card in self._cards_by_album.get(album_id, ()):
            card.clear_cover()

    @Slot(str)
    def _download_favorite(self, album_id: str) -> None:
        if self.download_controller is None:
            return
        self._chapter_flow.start(
            album_id,
            self._chapter_catalogs.get(album_id),
        )

    @Slot(str, bool)
    def _on_chapter_loading_changed(
        self,
        album_id: str,
        loading: bool,
    ) -> None:
        catalog = self._chapter_catalogs.get(album_id)
        for card in self._cards_by_album.get(album_id, ()):
            card.set_chapter_state(catalog, loading=loading)

    @Slot(str, object)
    def _on_chapter_catalog_resolved(
        self,
        album_id: str,
        catalog: ChapterCatalogSnapshot,
    ) -> None:
        self._chapter_catalogs[album_id] = catalog
        for card in self._cards_by_album.get(album_id, ()):
            card.set_chapter_state(catalog)

    @Slot(str, object)
    def _on_chapter_task_created(self, album_id: str, _snapshot) -> None:
        for card in self._cards_by_album.get(album_id, ()):
            card.set_task_present(True)

    @Slot(str, str)
    def _on_chapter_flow_failed(self, _album_id: str, message: str) -> None:
        QMessageBox.warning(self, "无法读取章节", message)

    @Slot(object)
    def _set_tasks(self, snapshots) -> None:
        self._tasks_by_album = {
            snapshot.album_id
            for snapshot in snapshots
            if hasattr(snapshot, "album_id")
        }
        for album_id, cards in self._cards_by_album.items():
            for card in cards:
                card.set_task_present(album_id in self._tasks_by_album)

    def _set_login_error(self, message: str) -> None:
        message = str(message).strip()
        self.login_error.setText(message)
        self.login_error.setVisible(bool(message))

    def _set_expired_error(self, message: str) -> None:
        message = str(message).strip()
        self.expired_error.setText(message)
        self.expired_error.setVisible(bool(message))

    def _set_favorites_error(self, message: str) -> None:
        self._set_favorites_feedback(message, error=True)

    def _set_favorites_feedback(
        self,
        message: str,
        *,
        error: bool,
    ) -> None:
        message = str(message).strip()
        self.favorites_error_label.setText(message)
        self.favorites_error_banner.setProperty("error", bool(error))
        self.favorites_error_banner.style().unpolish(
            self.favorites_error_banner
        )
        self.favorites_error_banner.style().polish(
            self.favorites_error_banner
        )
        self.favorites_error_banner.setVisible(bool(message))


def _display_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "未知"


__all__ = ["FAVORITES_PAGE_SIZE", "FavoritesPage"]
