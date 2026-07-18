from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import SearchMode, SearchPageSnapshot, SearchRequest, TaskStatus
from ..icons import arrow_icon, search_icon
from ..widgets.search_cover_loader import SearchCoverLoader
from ..widgets.search_result_card import SearchResultCard
from ..widgets.task_row import DownloadTaskRow
from ..widgets.thumbnail_loader import ThumbnailLoader
from .base import SectionPage

if TYPE_CHECKING:
    from ..controllers.download_controller import DownloadController
    from ..controllers.favorites_controller import FavoritesController
    from ..controllers.search_controller import SearchController


class DownloadPage(SectionPage):
    _MODE_LABELS = {
        SearchMode.GENERAL: "综合",
        SearchMode.AUTHOR: "作者",
        SearchMode.TAG: "标签",
    }
    _MODE_PLACEHOLDERS = {
        SearchMode.GENERAL: "搜索漫画名、标签或作者",
        SearchMode.AUTHOR: "搜索作者名称",
        SearchMode.TAG: "搜索标签",
    }

    def __init__(
        self,
        controller: "DownloadController | None" = None,
        parent=None,
        *,
        search_controller: "SearchController | None" = None,
        favorites_controller: "FavoritesController | None" = None,
        cover_loader: SearchCoverLoader | None = None,
    ):
        super().__init__("搜索与下载", "downloadPage", parent)
        self._controller = controller
        self._search_controller = search_controller
        self._favorites_controller = favorites_controller
        self._search_mode = SearchMode.GENERAL
        self._search_generation = 0
        self._search_snapshot: SearchPageSnapshot | None = None
        self._search_busy = False
        self._disposed = False
        self._task_rows = {}
        self._tasks_by_album = set()
        self._preview_requests = {}
        self._completion_scheduled = set()
        self._cards_by_album: dict[str, list[SearchResultCard]] = {}
        self._cover_attempted: set[tuple[int, str]] = set()
        self._cover_update_scheduled = False
        self._favorite_pending_album_id: str | None = None
        self._favorite_feedback_generation = 0
        self.comic_cards: tuple[SearchResultCard, ...] = ()
        self._column_count = 0

        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.thumbnail_ready.connect(self._on_thumbnail_ready)

        if cover_loader is not None:
            self._cover_loader = cover_loader
        elif (
            search_controller is not None
            and hasattr(search_controller, "service")
            and hasattr(search_controller.service, "fetch_cover")
        ):
            self._cover_loader = SearchCoverLoader(
                search_controller.service,
                self,
            )
        else:
            self._cover_loader = None
        if self._cover_loader is not None:
            self._cover_loader.cover_ready.connect(self._on_cover_ready)
            self._cover_loader.cover_failed.connect(self._on_cover_failed)

        self._create_search_bar()
        self._create_search_mode_row()

        self.view_tabs = QTabBar(self.content)
        self.view_tabs.setObjectName("downloadViewTabs")
        self.view_tabs.setDrawBase(False)
        self.view_tabs.setExpanding(False)
        self.view_tabs.addTab("搜索结果")
        self.view_tabs.addTab("下载任务 0")
        self.content_layout.addWidget(self.view_tabs)

        self.view_stack = QStackedWidget(self.content)
        self.view_stack.setObjectName("downloadViewStack")
        self.content_layout.addWidget(self.view_stack, 1)
        self.view_tabs.currentChanged.connect(self._on_view_changed)

        self._create_results_view()
        self._create_tasks_view()
        self.view_tabs.setCurrentIndex(0)

        if self._search_controller is not None:
            self._connect_search_controller()
        if self._favorites_controller is not None:
            self._connect_favorites_controller()
        if self._controller is not None:
            self._controller.tasks_reset.connect(self._set_tasks)
            self._controller.command_failed.connect(self._show_command_error)
            self._set_tasks(self._controller.list_tasks())

        search_enabled = self._search_controller is not None
        self.general_search_input.setEnabled(search_enabled)
        self.general_search_button.setEnabled(search_enabled)
        self.jm_id_search_input.setEnabled(search_enabled)
        self.jm_id_search_button.setEnabled(search_enabled)
        for button in self._mode_buttons.values():
            button.setEnabled(search_enabled)

        QTimer.singleShot(0, self._reflow_cards)

    def _create_search_bar(self) -> None:
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(10)

        search_button_icon = search_icon()

        self.general_search_input = QLineEdit(self.content)
        self.general_search_input.setObjectName("generalSearchInput")
        self.general_search_input.setPlaceholderText(
            self._MODE_PLACEHOLDERS[self._search_mode]
        )
        self.general_search_input.setClearButtonEnabled(True)
        self.general_search_input.setFixedHeight(42)
        self.general_search_input.returnPressed.connect(self._submit_general_search)
        search_layout.addWidget(self.general_search_input, 1)

        self.general_search_button = QToolButton(self.content)
        self.general_search_button.setObjectName("generalSearchButton")
        self.general_search_button.setIcon(search_button_icon)
        self.general_search_button.setToolTip("搜索")
        self.general_search_button.setFixedSize(42, 42)
        self.general_search_button.clicked.connect(self._submit_general_search)
        search_layout.addWidget(self.general_search_button)

        self.jm_id_search_input = QLineEdit(self.content)
        self.jm_id_search_input.setObjectName("jmIdSearchInput")
        self.jm_id_search_input.setPlaceholderText("精确 JM 号")
        self.jm_id_search_input.setClearButtonEnabled(True)
        self.jm_id_search_input.setFixedSize(150, 42)
        self.jm_id_search_input.returnPressed.connect(self._submit_exact_search)
        search_layout.addWidget(self.jm_id_search_input)

        self.jm_id_search_button = QToolButton(self.content)
        self.jm_id_search_button.setObjectName("jmIdSearchButton")
        self.jm_id_search_button.setIcon(search_button_icon)
        self.jm_id_search_button.setToolTip("精确查询")
        self.jm_id_search_button.setFixedSize(42, 42)
        self.jm_id_search_button.clicked.connect(self._submit_exact_search)
        search_layout.addWidget(self.jm_id_search_button)
        self.content_layout.addLayout(search_layout)

    def _create_search_mode_row(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        segment = QFrame(self.content)
        segment.setObjectName("searchModeSegment")
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(2, 2, 2, 2)
        segment_layout.setSpacing(0)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_buttons = {}
        for index, mode in enumerate(
            (SearchMode.GENERAL, SearchMode.AUTHOR, SearchMode.TAG)
        ):
            button = QToolButton(segment)
            button.setObjectName("searchModeButton")
            button.setText(self._MODE_LABELS[mode])
            button.setCheckable(True)
            button.setFixedSize(62, 32)
            button.clicked.connect(
                lambda checked=False, selected=mode: self._select_search_mode(
                    selected
                )
            )
            self._mode_group.addButton(button, index)
            self._mode_buttons[mode] = button
            segment_layout.addWidget(button)
        self._mode_buttons[self._search_mode].setChecked(True)
        row.addWidget(segment)

        row.addStretch(1)
        self.results_summary = QLabel(self.content)
        self.results_summary.setObjectName("searchResultsSummary")
        self.results_summary.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.results_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        row.addWidget(self.results_summary, 1)
        self.content_layout.addLayout(row)

    def _create_results_view(self) -> None:
        results_view = QWidget(self.view_stack)
        results_view.setObjectName("searchResultsView")
        results_layout = QVBoxLayout(results_view)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)

        self.results_state_stack = QStackedWidget(results_view)
        self.results_state_stack.setObjectName("searchStateStack")
        results_layout.addWidget(self.results_state_stack, 1)

        self._search_states = {
            "idle": self._create_state_page(
                "searchIdleState",
                "暂无搜索内容",
            ),
            "loading": self._create_state_page(
                "searchLoadingState",
                "正在搜索...",
                loading=True,
            ),
            "empty": self._create_state_page(
                "searchEmptyState",
                "没有找到匹配结果",
                retry=True,
            ),
            "error": self._create_state_page(
                "searchErrorState",
                "搜索暂时失败，请稍后重试",
                retry=True,
            ),
        }
        self.search_error_label = self._search_states["error"].findChild(
            QLabel,
            "searchStateLabel",
        )

        result_state = QWidget(self.results_state_stack)
        result_state.setObjectName("searchResultState")
        result_state_layout = QVBoxLayout(result_state)
        result_state_layout.setContentsMargins(0, 0, 0, 0)
        result_state_layout.setSpacing(8)

        self.page_error_banner = QFrame(result_state)
        self.page_error_banner.setObjectName("searchPageErrorBanner")
        banner_layout = QHBoxLayout(self.page_error_banner)
        banner_layout.setContentsMargins(10, 6, 8, 6)
        banner_layout.setSpacing(8)
        self.page_error_label = QLabel(self.page_error_banner)
        self.page_error_label.setObjectName("searchPageErrorLabel")
        self.page_error_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        banner_layout.addWidget(self.page_error_label, 1)
        self.page_retry_button = QPushButton("重试翻页", self.page_error_banner)
        self.page_retry_button.setObjectName("retrySearchPageButton")
        self.page_retry_button.setFixedHeight(30)
        self.page_retry_button.clicked.connect(self._retry_search)
        banner_layout.addWidget(self.page_retry_button)
        self.page_error_banner.hide()
        result_state_layout.addWidget(self.page_error_banner)

        self.favorite_feedback_banner = QFrame(result_state)
        self.favorite_feedback_banner.setObjectName("favoriteFeedbackBanner")
        feedback_layout = QHBoxLayout(self.favorite_feedback_banner)
        feedback_layout.setContentsMargins(10, 7, 10, 7)
        feedback_layout.setSpacing(0)
        self.favorite_feedback_label = QLabel(self.favorite_feedback_banner)
        self.favorite_feedback_label.setObjectName("favoriteFeedbackLabel")
        self.favorite_feedback_label.setTextFormat(Qt.TextFormat.PlainText)
        self.favorite_feedback_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        feedback_layout.addWidget(self.favorite_feedback_label, 1)
        self.favorite_feedback_banner.hide()
        result_state_layout.addWidget(self.favorite_feedback_banner)

        self.results_scroll = QScrollArea(result_state)
        self.results_scroll.setObjectName("resultsScroll")
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.results_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        self.results_canvas = QWidget(self.results_scroll)
        self.results_canvas.setObjectName("resultsCanvas")
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
        result_state_layout.addWidget(self.results_scroll, 1)

        self.pagination = QFrame(result_state)
        self.pagination.setObjectName("searchPagination")
        self.pagination.setFixedHeight(38)
        pagination_layout = QHBoxLayout(self.pagination)
        pagination_layout.setContentsMargins(0, 2, 0, 2)
        pagination_layout.setSpacing(12)
        pagination_layout.addStretch(1)

        self.previous_page_button = QToolButton(self.pagination)
        self.previous_page_button.setObjectName("searchPageButton")
        self.previous_page_button.setIcon(arrow_icon("left"))
        self.previous_page_button.setToolTip("上一页")
        self.previous_page_button.setFixedSize(32, 32)
        self.previous_page_button.clicked.connect(self._previous_page)
        pagination_layout.addWidget(self.previous_page_button)

        self.page_label = QLabel("第 1 / 1 页", self.pagination)
        self.page_label.setObjectName("searchPageLabel")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(96)
        pagination_layout.addWidget(self.page_label)

        self.next_page_button = QToolButton(self.pagination)
        self.next_page_button.setObjectName("searchPageButton")
        self.next_page_button.setIcon(arrow_icon("right"))
        self.next_page_button.setToolTip("下一页")
        self.next_page_button.setFixedSize(32, 32)
        self.next_page_button.clicked.connect(self._next_page)
        pagination_layout.addWidget(self.next_page_button)
        pagination_layout.addStretch(1)
        self.pagination.hide()
        result_state_layout.addWidget(self.pagination)

        self._search_states["results"] = result_state
        self.results_state_stack.addWidget(result_state)
        self._set_search_state("idle")
        self.view_stack.addWidget(results_view)

    def _create_state_page(
        self,
        object_name: str,
        message: str,
        *,
        loading: bool = False,
        retry: bool = False,
    ) -> QWidget:
        page = QWidget(self.results_state_stack)
        page.setObjectName(object_name)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addStretch(1)

        label = QLabel(message, page)
        label.setObjectName("searchStateLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)

        if loading:
            progress = QProgressBar(page)
            progress.setObjectName("searchLoadingBar")
            progress.setRange(0, 0)
            progress.setTextVisible(False)
            progress.setFixedSize(180, 4)
            layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignHCenter)
        if retry:
            button = QPushButton("重试", page)
            button.setObjectName("retrySearchButton")
            button.setFixedSize(88, 34)
            button.clicked.connect(self._retry_search)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)
        self.results_state_stack.addWidget(page)
        return page

    def _create_tasks_view(self) -> None:
        tasks_view = QWidget(self.view_stack)
        tasks_view.setObjectName("downloadTasksView")
        tasks_layout = QVBoxLayout(tasks_view)
        tasks_layout.setContentsMargins(0, 0, 0, 0)
        tasks_layout.setSpacing(12)

        command_layout = QHBoxLayout()
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(10)

        self.download_input = QLineEdit(tasks_view)
        self.download_input.setObjectName("downloadAlbumInput")
        self.download_input.setPlaceholderText("输入 JM 号")
        self.download_input.setClearButtonEnabled(True)
        self.download_input.setFixedHeight(42)
        self.download_input.returnPressed.connect(self._add_download_task)
        command_layout.addWidget(self.download_input, 1)

        self.download_button = QPushButton("开始下载", tasks_view)
        self.download_button.setObjectName("startDownloadButton")
        self.download_button.setFixedSize(112, 42)
        self.download_button.clicked.connect(self._add_download_task)
        command_layout.addWidget(self.download_button)
        tasks_layout.addLayout(command_layout)

        enabled = self._controller is not None
        self.download_input.setEnabled(enabled)
        self.download_button.setEnabled(enabled)

        self.tasks_scroll = QScrollArea(tasks_view)
        self.tasks_scroll.setObjectName("tasksScroll")
        self.tasks_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tasks_scroll.setWidgetResizable(True)
        self.tasks_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.tasks_canvas = QWidget(self.tasks_scroll)
        self.tasks_canvas.setObjectName("tasksCanvas")
        self.tasks_layout = QVBoxLayout(self.tasks_canvas)
        self.tasks_layout.setContentsMargins(0, 0, 8, 0)
        self.tasks_layout.setSpacing(8)
        self.tasks_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.empty_tasks_label = QLabel("还没有下载任务", self.tasks_canvas)
        self.empty_tasks_label.setObjectName("emptyTasksLabel")
        self.empty_tasks_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_tasks_label.setMinimumHeight(160)
        self.tasks_layout.addWidget(self.empty_tasks_label)
        self.tasks_scroll.setWidget(self.tasks_canvas)
        tasks_layout.addWidget(self.tasks_scroll, 1)
        self.view_stack.addWidget(tasks_view)

    def _connect_search_controller(self) -> None:
        controller = self._search_controller
        controller.search_submitted.connect(self._on_search_submitted)
        controller.results_ready.connect(self._on_search_results)
        controller.empty_results.connect(self._on_search_empty)
        controller.search_failed.connect(self._on_search_failed)
        controller.validation_failed.connect(self._on_search_validation_failed)
        controller.busy_changed.connect(self._on_search_busy_changed)

    def _connect_favorites_controller(self) -> None:
        controller = self._favorites_controller
        controller.add_succeeded.connect(self._on_favorite_added)
        controller.add_failed.connect(self._on_favorite_add_failed)
        controller.add_availability_changed.connect(
            self._on_favorite_availability_changed
        )
        controller.known_favorite_ids_changed.connect(
            self._on_known_favorite_ids_changed
        )
        controller.busy_changed.connect(self._on_favorites_busy_changed)

    @property
    def column_count(self) -> int:
        return self._column_count

    @property
    def search_mode(self) -> SearchMode:
        return self._search_mode

    @property
    def search_state(self) -> str:
        current = self.results_state_stack.currentWidget()
        for name, widget in self._search_states.items():
            if widget is current:
                return name
        return "unknown"

    def _set_search_state(self, state: str) -> None:
        self.results_state_stack.setCurrentWidget(self._search_states[state])

    def activate(self) -> None:
        self._schedule_visible_covers()

    def show_task(self, album_id: str) -> None:
        self._view_search_task(str(album_id))

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._cover_loader is not None:
            self._cover_loader.dispose()

    def eventFilter(self, watched, event):
        if (
            watched is self.results_scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._reflow_cards)
        return super().eventFilter(watched, event)

    def _select_search_mode(self, mode: SearchMode) -> None:
        self._search_mode = mode
        self.general_search_input.setPlaceholderText(
            self._MODE_PLACEHOLDERS[mode]
        )

    def _submit_general_search(self) -> None:
        if self._search_controller is None:
            return
        generation = self._search_controller.search(
            self._search_mode,
            self.general_search_input.text(),
            1,
        )
        if generation is None:
            self.general_search_input.setFocus()

    def _submit_exact_search(self) -> None:
        if self._search_controller is None:
            return
        generation = self._search_controller.search(
            SearchMode.EXACT_ID,
            self.jm_id_search_input.text(),
            1,
        )
        if generation is None:
            self.jm_id_search_input.setFocus()

    def _on_search_submitted(
        self,
        generation: int,
        request: SearchRequest,
    ) -> None:
        self._search_generation = generation
        self._clear_favorite_feedback()
        self._set_summary_error(False)
        self.page_error_banner.hide()
        current = self._search_snapshot
        is_page_change = bool(
            current is not None
            and current.request.mode is request.mode
            and current.request.query == request.query
            and current.request.page != request.page
        )
        if is_page_change:
            self.results_summary.setText(f"正在载入第 {request.page} 页...")
        else:
            self.results_summary.setText("正在搜索...")
            self._set_search_state("loading")

    def _on_search_results(
        self,
        generation: int,
        snapshot: SearchPageSnapshot,
        _is_page_change: bool,
    ) -> None:
        if generation != self._search_generation or self._disposed:
            return
        self._set_summary_error(False)
        self._search_snapshot = snapshot
        self._set_result_cards(snapshot)
        self._set_search_state("results")
        self.page_error_banner.hide()
        self._update_result_summary(snapshot)
        self._refresh_pagination()
        self.results_scroll.verticalScrollBar().setValue(0)
        self._schedule_visible_covers()

    def _on_search_empty(
        self,
        generation: int,
        snapshot: SearchPageSnapshot,
        _is_page_change: bool,
    ) -> None:
        if generation != self._search_generation or self._disposed:
            return
        self._set_summary_error(False)
        self._search_snapshot = snapshot
        self._set_result_cards(snapshot)
        self.results_summary.setText("共 0 条")
        self.page_error_banner.hide()
        self._set_search_state("empty")

    def _on_search_failed(
        self,
        generation: int,
        _code: str,
        message: str,
        is_page_change: bool,
    ) -> None:
        if generation != self._search_generation or self._disposed:
            return
        self._set_summary_error(False)
        if is_page_change and self._search_snapshot is not None:
            self.page_error_label.setText(message)
            self.page_error_banner.show()
            self._set_search_state("results")
            self._update_result_summary(self._search_snapshot)
            self._refresh_pagination()
            return
        self.search_error_label.setText(message)
        self.results_summary.clear()
        self._set_search_state("error")

    def _on_search_validation_failed(self, _code: str, message: str) -> None:
        self._set_summary_error(True)
        self.results_summary.setText(message)
        QTimer.singleShot(
            4000,
            lambda expected=message: self._clear_input_error(expected),
        )

    def _clear_input_error(self, expected: str) -> None:
        if self.results_summary.text() != expected:
            return
        self._set_summary_error(False)
        if self.search_state == "results" and self._search_snapshot is not None:
            self._update_result_summary(self._search_snapshot)
        elif self.search_state == "empty":
            self.results_summary.setText("共 0 条")
        else:
            self.results_summary.clear()

    def _set_summary_error(self, error: bool) -> None:
        self.results_summary.setProperty("error", bool(error))
        self.results_summary.style().unpolish(self.results_summary)
        self.results_summary.style().polish(self.results_summary)

    def _on_search_busy_changed(self, busy: bool) -> None:
        self._search_busy = bool(busy)
        self.results_summary.setProperty("busy", self._search_busy)
        self.results_summary.style().unpolish(self.results_summary)
        self.results_summary.style().polish(self.results_summary)
        self._refresh_pagination()
        self._refresh_card_actions()

    def _retry_search(self) -> None:
        if self._search_controller is not None:
            self._search_controller.retry()

    def _previous_page(self) -> None:
        if self._search_controller is None or self._search_snapshot is None:
            return
        self._search_controller.change_page(
            self._search_snapshot.request.page - 1
        )

    def _next_page(self) -> None:
        if self._search_controller is None or self._search_snapshot is None:
            return
        self._search_controller.change_page(
            self._search_snapshot.request.page + 1
        )

    def _refresh_pagination(self) -> None:
        snapshot = self._search_snapshot
        if snapshot is None:
            self.pagination.hide()
            self.previous_page_button.setEnabled(False)
            self.next_page_button.setEnabled(False)
            return
        show_pagination = (
            snapshot.request.mode is not SearchMode.EXACT_ID
            and snapshot.page_count > 1
        )
        self.pagination.setVisible(show_pagination)
        current_page = snapshot.request.page
        page_count = max(1, snapshot.page_count)
        self.page_label.setText(f"第 {current_page} / {page_count} 页")
        self.previous_page_button.setEnabled(
            not self._search_busy and current_page > 1
        )
        self.next_page_button.setEnabled(
            not self._search_busy
            and snapshot.page_count > 0
            and current_page < snapshot.page_count
        )

    def _update_result_summary(self, snapshot: SearchPageSnapshot) -> None:
        total_text = (
            f"最多展示 {snapshot.total} 条"
            if snapshot.truncated
            else f"共 {snapshot.total} 条"
        )
        if (
            snapshot.request.mode is SearchMode.EXACT_ID
            or snapshot.page_count <= 1
        ):
            self.results_summary.setText(total_text)
            return
        self.results_summary.setText(
            f"{total_text} · 第 {snapshot.request.page} / "
            f"{max(1, snapshot.page_count)} 页"
        )

    def _set_result_cards(self, snapshot: SearchPageSnapshot) -> None:
        while self.results_grid.count():
            self.results_grid.takeAt(0)
        for card in self.comic_cards:
            card.hide()
            card.deleteLater()

        cards = []
        self._cards_by_album = {}
        self._cover_attempted.clear()
        for item in snapshot.items:
            card = SearchResultCard(item, self.results_canvas)
            card.set_task_present(item.album_id in self._tasks_by_album)
            card.action_button.setEnabled(
                self._controller is not None and not self._search_busy
            )
            card.download_requested.connect(self._download_search_result)
            card.view_task_requested.connect(self._view_search_task)
            if self._favorites_controller is not None:
                card.set_favorite_visible(True)
                card.favorite_requested.connect(self._add_search_favorite)
                self._render_card_favorite_state(card)
            cards.append(card)
            self._cards_by_album.setdefault(item.album_id, []).append(card)
        self.comic_cards = tuple(cards)
        self._column_count = 0
        self._reflow_cards()

    def _refresh_card_actions(self) -> None:
        enabled = self._controller is not None and not self._search_busy
        for card in self.comic_cards:
            card.action_button.setEnabled(enabled)

    def _add_search_favorite(self, album_id: str) -> None:
        if self._favorites_controller is None:
            return
        self._clear_favorite_feedback()
        generation = self._favorites_controller.add_album(album_id)
        if generation is None:
            self._refresh_favorite_cards()
            return
        self._favorite_pending_album_id = album_id
        self._refresh_favorite_cards()

    def _on_favorite_added(self, album_id: str) -> None:
        if self._disposed:
            return
        if self._favorite_pending_album_id == album_id:
            self._favorite_pending_album_id = None
        self._refresh_favorite_cards()
        self._show_favorite_feedback(
            "已添加到默认收藏，请在我的收藏中手动同步",
            error=False,
        )

    def _on_favorite_add_failed(
        self,
        album_id: str,
        _code: str,
        message: str,
    ) -> None:
        if self._disposed:
            return
        if not album_id or self._favorite_pending_album_id == album_id:
            self._favorite_pending_album_id = None
        self._refresh_favorite_cards()
        self._show_favorite_feedback(message, error=True)

    def _on_favorite_availability_changed(self, _available: bool) -> None:
        self._refresh_favorite_cards()

    def _on_known_favorite_ids_changed(self, _album_ids) -> None:
        self._refresh_favorite_cards()

    def _on_favorites_busy_changed(self, busy: bool, _command: str) -> None:
        if not busy:
            QTimer.singleShot(0, self._clear_stale_favorite_pending)
        self._refresh_favorite_cards()

    def _clear_stale_favorite_pending(self) -> None:
        if (
            self._favorites_controller is None
            or self._favorites_controller.is_busy
        ):
            return
        self._favorite_pending_album_id = None
        self._refresh_favorite_cards()

    def _refresh_favorite_cards(self) -> None:
        if self._disposed:
            return
        for card in self.comic_cards:
            self._render_card_favorite_state(card)

    def _render_card_favorite_state(self, card: SearchResultCard) -> None:
        controller = self._favorites_controller
        if controller is None:
            card.set_favorite_visible(False)
            return
        album_id = card.snapshot.album_id
        card.set_favorite_state(
            available=controller.can_add_favorites,
            busy=(
                controller.is_busy
                and controller.current_command == "add"
                and self._favorite_pending_album_id == album_id
            ),
            favorited=album_id in controller.known_favorite_ids,
        )

    def _show_favorite_feedback(self, message: str, *, error: bool) -> None:
        self._favorite_feedback_generation += 1
        generation = self._favorite_feedback_generation
        self.favorite_feedback_label.setText(message)
        self.favorite_feedback_banner.setProperty("error", bool(error))
        self.favorite_feedback_banner.style().unpolish(
            self.favorite_feedback_banner
        )
        self.favorite_feedback_banner.style().polish(
            self.favorite_feedback_banner
        )
        self.favorite_feedback_banner.show()
        QTimer.singleShot(
            5000,
            lambda expected=generation: self._expire_favorite_feedback(
                expected
            ),
        )

    def _clear_favorite_feedback(self) -> None:
        self._favorite_feedback_generation += 1
        if hasattr(self, "favorite_feedback_banner"):
            self.favorite_feedback_banner.hide()

    def _expire_favorite_feedback(self, expected: int) -> None:
        if self._disposed or expected != self._favorite_feedback_generation:
            return
        self.favorite_feedback_banner.hide()

    def _reflow_cards(self) -> None:
        if not hasattr(self, "results_scroll"):
            return
        available_width = max(1, self.results_scroll.viewport().width() - 8)
        spacing = self.results_grid.horizontalSpacing()
        step = SearchResultCard.WIDTH + spacing
        columns = max(1, (available_width + spacing) // step)
        if (
            columns == self._column_count
            and self.results_grid.count() == len(self.comic_cards)
        ):
            self._schedule_visible_covers()
            return

        while self.results_grid.count():
            self.results_grid.takeAt(0)
        for index, card in enumerate(self.comic_cards):
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
            or not self.comic_cards
            or self.view_tabs.currentIndex() != 0
            or self.search_state != "results"
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
            (len(self.comic_cards) - 1) // columns,
            last_visible_row + 1,
        )
        start = first_row * columns
        stop = min(len(self.comic_cards), (final_row + 1) * columns)
        target_size = QSize(
            SearchResultCard.WIDTH - 2,
            SearchResultCard.COVER_HEIGHT,
        )
        for card in self.comic_cards[start:stop]:
            key = (self._search_generation, card.snapshot.album_id)
            if key in self._cover_attempted:
                continue
            if self._cover_loader.request(
                self._search_generation,
                card.snapshot.album_id,
                target_size,
            ):
                self._cover_attempted.add(key)

    def _on_cover_ready(self, generation: int, album_id: str, image) -> None:
        if generation != self._search_generation or self._disposed:
            return
        for card in self._cards_by_album.get(album_id, ()):
            card.set_cover(image)

    def _on_cover_failed(self, generation: int, album_id: str) -> None:
        if generation != self._search_generation or self._disposed:
            return
        for card in self._cards_by_album.get(album_id, ()):
            card.clear_cover()

    def _download_search_result(self, album_id: str) -> None:
        if self._controller is None:
            return
        snapshot = self._controller.add_task(album_id)
        if snapshot is None:
            return
        for card in self._cards_by_album.get(album_id, ()):
            card.set_task_present(True)

    def _view_search_task(self, album_id: str) -> None:
        self.view_tabs.setCurrentIndex(1)
        for row in self._task_rows.values():
            if row.snapshot.album_id == album_id:
                self.tasks_scroll.ensureWidgetVisible(row)
                break

    def _on_view_changed(self, index: int) -> None:
        self.view_stack.setCurrentIndex(index)
        if index == 0:
            self._schedule_visible_covers()

    def _add_download_task(self) -> None:
        if self._controller is None:
            return
        snapshot = self._controller.add_task(self.download_input.text())
        if snapshot is None:
            self.download_input.setFocus()
            return
        self.download_input.clear()
        self.view_tabs.setCurrentIndex(1)

    def _set_tasks(self, snapshots) -> None:
        snapshots = list(snapshots)
        task_ids = {snapshot.id for snapshot in snapshots}
        self._tasks_by_album = {snapshot.album_id for snapshot in snapshots}
        for task_id in tuple(self._task_rows):
            if task_id in task_ids:
                continue
            row = self._task_rows.pop(task_id)
            self._preview_requests.pop(task_id, None)
            self._thumbnail_loader.clear_task(task_id)
            self._completion_scheduled.discard(task_id)
            row.hide()
            row.deleteLater()

        for snapshot in snapshots:
            row = self._task_rows.get(snapshot.id)
            if row is None:
                row = DownloadTaskRow(snapshot, self.tasks_canvas)
                row.pause_requested.connect(self._controller.pause_task)
                row.resume_requested.connect(self._controller.resume_task)
                row.cancel_requested.connect(self._confirm_cancel)
                row.retry_requested.connect(self._controller.retry_task)
                row.remove_requested.connect(self._controller.remove_task)
                row.open_requested.connect(self._controller.open_task_item)
                self._task_rows[snapshot.id] = row
            else:
                row.update_snapshot(snapshot)

            if (
                snapshot.status == TaskStatus.COMPLETED
                and snapshot.id not in self._completion_scheduled
            ):
                self._completion_scheduled.add(snapshot.id)
                QTimer.singleShot(
                    5000,
                    lambda task_id=snapshot.id: self._expire_completed_task(
                        task_id
                    ),
                )

            if snapshot.preview_path is not None:
                preview_key = (
                    snapshot.preview_revision,
                    str(snapshot.preview_path),
                )
                if self._preview_requests.get(snapshot.id) != preview_key:
                    self._preview_requests[snapshot.id] = preview_key
                    self._thumbnail_loader.request(
                        snapshot.id,
                        snapshot.preview_revision,
                        snapshot.preview_path,
                        QSize(144, 200),
                    )

        while self.tasks_layout.count():
            self.tasks_layout.takeAt(0)
        if snapshots:
            self.empty_tasks_label.hide()
            for snapshot in snapshots:
                self.tasks_layout.addWidget(self._task_rows[snapshot.id])
        else:
            self.empty_tasks_label.show()
            self.tasks_layout.addWidget(self.empty_tasks_label)
        self.tasks_layout.addStretch(1)
        self.view_tabs.setTabText(1, f"下载任务 {len(snapshots)}")

        for album_id, cards in self._cards_by_album.items():
            task_present = album_id in self._tasks_by_album
            for card in cards:
                card.set_task_present(task_present)

    def _on_thumbnail_ready(self, task_id: str, revision: int, image) -> None:
        row = self._task_rows.get(task_id)
        if row is None or row.snapshot.preview_revision != revision:
            return
        row.set_preview(image, revision)

    def _show_command_error(self, _command: str, message: str) -> None:
        QMessageBox.warning(self, "操作失败", message)

    def _confirm_cancel(self, task_id: str) -> None:
        row = self._task_rows.get(task_id)
        if row is None or self._controller is None:
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("取消下载任务")
        dialog.setText(f"取消 JM {row.snapshot.album_id} 的下载任务？")
        dialog.setInformativeText(
            "可以只移除任务并保留现有文件，或同时删除已经下载的图片和 PDF。"
        )
        keep_button = dialog.addButton(
            "仅移除任务",
            QMessageBox.ButtonRole.AcceptRole,
        )
        delete_button = dialog.addButton(
            "移除并删除文件",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        back_button = dialog.addButton(
            "返回",
            QMessageBox.ButtonRole.RejectRole,
        )
        dialog.setDefaultButton(keep_button)
        dialog.setEscapeButton(back_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is keep_button:
            self._controller.cancel_task(task_id, False)
        elif clicked is delete_button:
            self._controller.cancel_task(task_id, True)

    def _expire_completed_task(self, task_id: str) -> None:
        self._completion_scheduled.discard(task_id)
        row = self._task_rows.get(task_id)
        if (
            row is None
            or row.snapshot.status != TaskStatus.COMPLETED
            or self._controller is None
        ):
            return
        self._controller.remove_task(task_id)
