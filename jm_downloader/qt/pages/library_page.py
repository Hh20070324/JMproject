from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from shiboken6 import isValid
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
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
    ChapterCatalogSnapshot,
    ChapterImageStatus,
    ChapterPackageStatus,
    ChapterSnapshot,
    LibraryItem,
    ReaderHistoryEntry,
    SearchResultSnapshot,
    TaskConfig,
)
from ..icons import svg_icon
from ..widgets.library_chapter_dialogs import (
    LegacyMigrationPreviewDialog,
    LibraryChapterDialog,
    PackageFormatConfirmationDialog,
)
from ..widgets.library_item_card import LibraryItemCard
from ..widgets.thumbnail_loader import ThumbnailLoader
from .base import SectionPage

if TYPE_CHECKING:
    from ..controllers.chapter_catalog_controller import ChapterCatalogController
    from ..controllers.library_controller import LibraryController
    from ..controllers.settings_controller import SettingsController


class LibraryPage(SectionPage):
    view_task_requested = Signal(str)
    local_read_ready = Signal(object, object)
    local_read_failed = Signal(object, str)

    FILTERS = (
        ("all", "全部"),
        ("images", "有图片"),
        ("pdf", "有打包产物"),
    )

    def __init__(
        self,
        controller: "LibraryController | None" = None,
        parent=None,
        *,
        chapter_catalog_controller: "ChapterCatalogController | None" = None,
        settings_controller: "SettingsController | None" = None,
    ):
        super().__init__("本地漫画库", "libraryPage", parent)
        self._controller = controller
        self._chapter_catalog_controller = chapter_catalog_controller
        self._settings_controller = settings_controller
        self._chapter_dialogs = {}
        self._chapter_requests = {}
        self._local_read_requests = {}
        self._catalog_requests = {}
        self._chapter_completion_messages = {}
        self._items: list[LibraryItem] = []
        self._rows = {}
        self._preview_state = {}
        self._active_albums = frozenset()
        self._busy_albums = frozenset()
        self._visible_ids = ()
        self._selected_ids = set()
        self._selection_mode = False
        self._sort_mode = "downloaded_desc"
        self._column_count = 0
        self._toolbar_compact = None
        self._loading = False
        self._has_loaded = False
        self._scan_error = None
        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.thumbnail_ready.connect(self._on_thumbnail_ready)

        self._create_toolbar()

        self.loading_bar = QProgressBar(self.content)
        self.loading_bar.setObjectName("libraryLoadingBar")
        self.loading_bar.setRange(0, 0)
        self.loading_bar.setTextVisible(False)
        self.loading_bar.setFixedHeight(3)
        self.loading_bar.hide()
        self.content_layout.addWidget(self.loading_bar)

        self._create_error_banner()
        self._create_content()

        if self._controller is not None:
            self._controller.items_reset.connect(self._set_items)
            self._controller.loading_changed.connect(self._set_loading)
            self._controller.busy_albums_changed.connect(self._set_busy_albums)
            self._controller.active_albums_changed.connect(
                self._set_active_albums
            )
            self._controller.command_failed.connect(self._show_command_error)
            if hasattr(self._controller, "batch_delete_finished"):
                self._controller.batch_delete_finished.connect(
                    self._on_batch_delete_finished
                )
            if hasattr(self._controller, "request_completed"):
                self._controller.request_completed.connect(
                    self._on_library_request_completed
                )
                self._controller.request_failed.connect(
                    self._on_library_request_failed
                )
            self._items = self._controller.list_items()
            self._has_loaded = bool(self._items)
            self._active_albums = self._controller.active_album_ids()
            self._busy_albums = self._controller.busy_album_ids()
        else:
            self.search_input.setEnabled(False)
            for button in self._filter_buttons.values():
                button.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.sort_button.setEnabled(False)
            self.select_button.setEnabled(False)

        if self._chapter_catalog_controller is not None:
            self._chapter_catalog_controller.catalog_ready.connect(
                self._on_catalog_ready
            )
            self._chapter_catalog_controller.catalog_failed.connect(
                self._on_catalog_failed
            )

        self._sync_rows()
        self._apply_filter(force=True)
        QTimer.singleShot(0, self._reflow_toolbar)

    def _create_toolbar(self) -> None:
        self.toolbar = QWidget(self.content)
        self.toolbar.setObjectName("libraryToolbar")
        self.toolbar_grid = QGridLayout(self.toolbar)
        self.toolbar_grid.setContentsMargins(0, 0, 0, 0)
        self.toolbar_grid.setHorizontalSpacing(10)
        self.toolbar_grid.setVerticalSpacing(8)

        self.search_input = QLineEdit(self.toolbar)
        self.search_input.setObjectName("librarySearchInput")
        self.search_input.setPlaceholderText("搜索 JM 号或漫画名称")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(42)
        self.search_input.textChanged.connect(self._apply_filter)

        self.filter_segment = QFrame(self.toolbar)
        self.filter_segment.setObjectName("libraryFilterSegment")
        filter_layout = QHBoxLayout(self.filter_segment)
        filter_layout.setContentsMargins(3, 3, 3, 3)
        filter_layout.setSpacing(2)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons = {}
        for index, (value, text) in enumerate(self.FILTERS):
            button = QToolButton(self.filter_segment)
            button.setObjectName("libraryFilterButton")
            button.setProperty("filter", value)
            button.setText(text)
            button.setCheckable(True)
            button.setFixedHeight(34)
            button.clicked.connect(self._apply_filter)
            self._filter_group.addButton(button, index)
            self._filter_buttons[value] = button
            filter_layout.addWidget(button)
        self._resize_filter_buttons()
        self._filter_buttons["all"].setChecked(True)

        self.sort_button = QToolButton(self.toolbar)
        self.sort_button.setObjectName("librarySortButton")
        self.sort_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.sort_button.setFixedSize(132, 38)
        self.sort_menu = QMenu(self.sort_button)
        self.sort_menu.setObjectName("librarySortMenu")
        self._sort_actions = {}
        for value, text in (
            ("downloaded_desc", "下载时间（新到旧）"),
            ("name_asc", "名称（A–Z）"),
        ):
            action = QAction(text, self.sort_menu)
            action.setCheckable(True)
            action.setData(value)
            action.triggered.connect(
                lambda _checked=False, mode=value: self._set_sort_mode(mode)
            )
            self.sort_menu.addAction(action)
            self._sort_actions[value] = action
        self.sort_button.setMenu(self.sort_menu)
        self._set_sort_mode(self._sort_mode, apply=False)

        self.select_button = QToolButton(self.toolbar)
        self.select_button.setObjectName("librarySelectButton")
        self.select_button.setText("选择")
        self.select_button.setCheckable(True)
        self.select_button.setFixedSize(70, 38)
        self.select_button.toggled.connect(self._set_selection_mode)

        self.refresh_button = QToolButton(self.toolbar)
        self.refresh_button.setObjectName("refreshLibraryButton")
        self.refresh_button.setToolTip("刷新本地库")
        self.refresh_button.setIcon(svg_icon("refresh"))
        self.refresh_button.setFixedSize(38, 38)
        self.refresh_button.clicked.connect(self.refresh)

        self.count_label = QLabel("0 / 0 本", self.toolbar)
        self.count_label.setObjectName("libraryCountLabel")
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.count_label.setMinimumWidth(84)
        self.content_layout.addWidget(self.toolbar)

        self.selection_bar = QFrame(self.content)
        self.selection_bar.setObjectName("librarySelectionBar")
        selection_layout = QHBoxLayout(self.selection_bar)
        selection_layout.setContentsMargins(10, 6, 8, 6)
        selection_layout.setSpacing(8)
        self.selected_count_label = QLabel("已选 0 本", self.selection_bar)
        self.selected_count_label.setObjectName("librarySelectedCount")
        selection_layout.addWidget(self.selected_count_label)
        selection_layout.addStretch(1)
        self.delete_selected_button = QToolButton(self.selection_bar)
        self.delete_selected_button.setObjectName(
            "libraryDeleteSelectedButton"
        )
        self.delete_selected_button.setText("删除所选")
        self.delete_selected_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.delete_selected_menu = QMenu(
            self.delete_selected_button
        )
        self.delete_selected_menu.setObjectName(
            "libraryDeleteSelectedMenu"
        )
        for kind, text in (
            ("images", "删除全部图片"),
            ("pdf", "删除全部打包产物"),
            ("all", "删除全部"),
        ):
            action = QAction(text, self.delete_selected_menu)
            action.triggered.connect(
                lambda _checked=False, value=kind: (
                    self._confirm_batch_delete(value)
                )
            )
            self.delete_selected_menu.addAction(action)
        self.delete_selected_button.setMenu(self.delete_selected_menu)
        selection_layout.addWidget(self.delete_selected_button)
        self.cancel_selection_button = QToolButton(self.selection_bar)
        self.cancel_selection_button.setObjectName(
            "libraryCancelSelectionButton"
        )
        self.cancel_selection_button.setText("取消")
        self.cancel_selection_button.clicked.connect(
            lambda: self.select_button.setChecked(False)
        )
        selection_layout.addWidget(self.cancel_selection_button)
        self.selection_bar.hide()
        self.content_layout.addWidget(self.selection_bar)

    def _create_error_banner(self) -> None:
        self.error_banner = QFrame(self.content)
        self.error_banner.setObjectName("libraryErrorBanner")
        banner_layout = QHBoxLayout(self.error_banner)
        banner_layout.setContentsMargins(10, 7, 10, 7)
        banner_layout.setSpacing(8)
        self.error_label = QLabel(self.error_banner)
        self.error_label.setObjectName("libraryErrorLabel")
        self.error_label.setWordWrap(True)
        banner_layout.addWidget(self.error_label, 1)
        self.error_banner.hide()
        self.content_layout.addWidget(self.error_banner)

    def _create_content(self) -> None:
        self.content_stack = QStackedWidget(self.content)
        self.content_stack.setObjectName("libraryContentStack")
        self.content_layout.addWidget(self.content_stack, 1)

        self.state_panel = QWidget(self.content_stack)
        self.state_panel.setObjectName("libraryStatePanel")
        state_layout = QVBoxLayout(self.state_panel)
        state_layout.setContentsMargins(20, 20, 20, 20)
        state_layout.setSpacing(12)
        state_layout.addStretch(1)
        self.state_label = QLabel(self.state_panel)
        self.state_label.setObjectName("libraryEmptyLabel")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        state_layout.addWidget(self.state_label)
        self.retry_button = QPushButton("重新加载", self.state_panel)
        self.retry_button.setObjectName("retryLibraryButton")
        self.retry_button.setFixedSize(104, 36)
        self.retry_button.clicked.connect(self.refresh)
        state_layout.addWidget(
            self.retry_button,
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        state_layout.addStretch(1)
        self.content_stack.addWidget(self.state_panel)

        self.scroll_area = QScrollArea(self.content_stack)
        self.scroll_area.setObjectName("libraryScrollArea")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.library_canvas = QWidget(self.scroll_area)
        self.library_canvas.setObjectName("libraryCanvas")
        self.library_grid = QGridLayout(self.library_canvas)
        self.library_grid.setContentsMargins(0, 0, 8, 0)
        self.library_grid.setHorizontalSpacing(12)
        self.library_grid.setVerticalSpacing(12)
        self.library_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.scroll_area.setWidget(self.library_canvas)
        self.scroll_area.viewport().installEventFilter(self)
        self.content_stack.addWidget(self.scroll_area)

    @property
    def column_count(self) -> int:
        return self._column_count

    @property
    def visible_album_ids(self) -> tuple[str, ...]:
        return self._visible_ids

    def filter_button(self, value: str) -> QToolButton:
        return self._filter_buttons[value]

    def item_card(self, album_id: str) -> LibraryItemCard:
        return self._rows[album_id]

    def activate(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._controller is not None:
            self._controller.refresh()

    def eventFilter(self, watched, event):
        if (
            watched is self.scroll_area.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._reflow_cards)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._reflow_toolbar)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._resize_filter_buttons()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        ):
            self._resize_filter_buttons()

    def _resize_filter_buttons(self) -> None:
        buttons = getattr(self, "_filter_buttons", {})
        for _value, button in buttons.items():
            width = max(
                52,
                button.fontMetrics().horizontalAdvance(button.text()) + 28,
            )
            button.setFixedSize(width, 34)

    def _reflow_toolbar(self) -> None:
        compact = self.content.width() < 700
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        if compact:
            self.toolbar.setFixedHeight(88)
            self.toolbar_grid.addWidget(self.search_input, 0, 0, 1, 4)
            self.toolbar_grid.addWidget(self.count_label, 0, 4)
            self.toolbar_grid.addWidget(self.filter_segment, 1, 0, 1, 2)
            self.toolbar_grid.addWidget(self.sort_button, 1, 2)
            self.toolbar_grid.addWidget(self.select_button, 1, 3)
            self.toolbar_grid.addWidget(self.refresh_button, 1, 4)
        else:
            self.toolbar.setFixedHeight(42)
            self.toolbar_grid.addWidget(self.search_input, 0, 0)
            self.toolbar_grid.addWidget(self.filter_segment, 0, 1)
            self.toolbar_grid.addWidget(self.sort_button, 0, 2)
            self.toolbar_grid.addWidget(self.select_button, 0, 3)
            self.toolbar_grid.addWidget(self.refresh_button, 0, 4)
            self.toolbar_grid.addWidget(self.count_label, 0, 5)
        self.toolbar_grid.setColumnStretch(0, 1)
        self.toolbar_grid.setColumnStretch(1, 0)
        self.toolbar_grid.setColumnStretch(2, 0)
        self.toolbar_grid.setColumnStretch(3, 0)
        self.toolbar_grid.setColumnStretch(4, 0)
        self.toolbar_grid.setColumnStretch(5, 0)

    def _set_items(self, items) -> None:
        self._items = list(items)
        self._has_loaded = True
        self._scan_error = None
        self.error_banner.hide()
        self._sync_rows()
        self._apply_filter(force=True)

    def _sync_rows(self) -> None:
        item_ids = {item.album_id for item in self._items}
        self._selected_ids.intersection_update(item_ids)
        for album_id in tuple(self._rows):
            if album_id in item_ids:
                continue
            row = self._rows.pop(album_id)
            self._preview_state.pop(album_id, None)
            self._thumbnail_loader.clear_task(album_id)
            row.hide()
            row.deleteLater()

        for item in self._items:
            row = self._rows.get(item.album_id)
            if row is None:
                row = LibraryItemCard(item, self.library_canvas)
                row.open_requested.connect(self._open_item)
                row.view_task_requested.connect(
                    self.view_task_requested.emit
                )
                row.read_requested.connect(self.prepare_local_read)
                row.delete_requested.connect(self._confirm_delete)
                row.chapter_action_requested.connect(
                    self._open_chapter_action
                )
                row.selection_changed.connect(
                    self._on_selection_changed
                )
                self._rows[item.album_id] = row
            else:
                row.update_item(item)
            row.set_selection_mode(self._selection_mode)
            row.set_selected(item.album_id in self._selected_ids)
            row.set_activity(
                item.album_id in self._active_albums,
                item.album_id in self._busy_albums,
            )
            self._queue_preview(item, row)
        self._sync_selection_controls()

    def _queue_preview(self, item: LibraryItem, row: LibraryItemCard) -> None:
        path = item.preview_path
        if path is None:
            if item.album_id in self._preview_state:
                self._thumbnail_loader.clear_task(item.album_id)
                self._preview_state.pop(item.album_id, None)
            row.reset_preview()
            return

        fingerprint = self._preview_fingerprint(path)
        current = self._preview_state.get(item.album_id)
        if current is not None and current[0] == fingerprint:
            return
        revision = 1 if current is None else current[1] + 1
        if current is not None:
            self._thumbnail_loader.clear_task(item.album_id)
            row.reset_preview()
        self._preview_state[item.album_id] = (fingerprint, revision)
        self._thumbnail_loader.request(
            item.album_id,
            revision,
            path,
            QSize(192, 280),
        )

    @staticmethod
    def _preview_fingerprint(path: Path) -> tuple[str, int, int]:
        resolved = Path(path).resolve()
        try:
            stat = resolved.stat()
            return str(resolved), stat.st_mtime_ns, stat.st_size
        except OSError:
            return str(resolved), -1, -1

    def _apply_filter(self, *_args, force: bool = False) -> None:
        query = " ".join(
            self.search_input.text().split()
        ).casefold()
        id_query = query
        if id_query.startswith("#"):
            id_query = id_query[1:].strip()
        if id_query.startswith("jm"):
            candidate = id_query[2:].strip()
            if candidate.isdigit():
                id_query = candidate
        selected = next(
            (
                value
                for value, button in self._filter_buttons.items()
                if button.isChecked()
            ),
            "all",
        )
        visible = [
            item
            for item in self._items
            if (
                not query
                or id_query in item.album_id.casefold()
                or (
                    item.title is not None
                    and query
                    in " ".join(item.title.split()).casefold()
                )
            )
            and (
                selected == "all"
                or (selected == "images" and item.has_images)
                or (
                    selected == "pdf"
                    and (item.has_pdf or item.has_cbz)
                )
            )
        ]
        if self._sort_mode == "downloaded_desc":
            visible.sort(
                key=lambda item: self._download_timestamp(
                    item.downloaded_at_utc
                ),
                reverse=True,
            )
        else:
            visible.sort(
                key=lambda item: (
                    (
                        " ".join(item.title.split()).casefold()
                        if item.title
                        else "\uffff"
                    ),
                    int(item.album_id),
                )
            )
        self._visible_ids = tuple(item.album_id for item in visible)
        self.count_label.setText(f"{len(visible)} / {len(self._items)} 本")
        self._reflow_cards(force=True if force else False)
        self._sync_content_state()

    @staticmethod
    def _download_timestamp(value: str | None) -> float:
        if not value:
            return float("-inf")
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00"
                if value.endswith("Z")
                else value
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OSError, OverflowError):
            return float("-inf")

    def _set_sort_mode(
        self,
        mode: str,
        *,
        apply: bool = True,
    ) -> None:
        if mode not in self._sort_actions:
            return
        self._sort_mode = mode
        for value, action in self._sort_actions.items():
            action.setChecked(value == mode)
        self.sort_button.setText(
            "下载时间 ▾" if mode == "downloaded_desc" else "名称 ▾"
        )
        if apply:
            self._apply_filter(force=True)

    def _set_selection_mode(self, enabled: bool) -> None:
        self._selection_mode = bool(enabled)
        self.select_button.setText(
            "选择中" if self._selection_mode else "选择"
        )
        self.selection_bar.setVisible(self._selection_mode)
        if not self._selection_mode:
            self._selected_ids.clear()
        for row in self._rows.values():
            row.set_selection_mode(self._selection_mode)
            row.set_selected(
                row.item.album_id in self._selected_ids
            )
        self._sync_selection_controls()

    def _on_selection_changed(
        self,
        album_id: str,
        selected: bool,
    ) -> None:
        if selected:
            if (
                album_id in self._active_albums
                or album_id in self._busy_albums
            ):
                return
            self._selected_ids.add(album_id)
        else:
            self._selected_ids.discard(album_id)
        self._sync_selection_controls()

    def _sync_selection_controls(self) -> None:
        self.selected_count_label.setText(
            f"已选 {len(self._selected_ids)} 本"
        )
        self.delete_selected_button.setEnabled(
            bool(self._selected_ids)
        )

    def _reflow_cards(self, force: bool = False) -> None:
        available = max(1, self.scroll_area.viewport().width() - 8)
        card_width = max(
            340,
            max(
                (
                    row.minimumSizeHint().width()
                    for row in self._rows.values()
                ),
                default=340,
            ),
        )
        two_column_width = card_width * 2 + self.library_grid.horizontalSpacing()
        columns = 2 if available >= two_column_width else 1
        if not force and columns == self._column_count:
            current_ids = tuple(
                self.library_grid.itemAt(index).widget().item.album_id
                for index in range(self.library_grid.count())
                if isinstance(self.library_grid.itemAt(index).widget(), LibraryItemCard)
            )
            if current_ids == self._visible_ids:
                return

        while self.library_grid.count():
            self.library_grid.takeAt(0)
        for row in self._rows.values():
            row.hide()
        for index, album_id in enumerate(self._visible_ids):
            row = self._rows[album_id]
            row.show()
            self.library_grid.addWidget(row, index // columns, index % columns)
        for column in range(2):
            self.library_grid.setColumnStretch(column, 1 if column < columns else 0)
        self._column_count = columns

    def _sync_content_state(self) -> None:
        if self._visible_ids:
            self.content_stack.setCurrentWidget(self.scroll_area)
            return
        if self._loading and not self._has_loaded:
            text = "正在扫描本地文件"
            retry = False
        elif self._scan_error and not self._items:
            text = "本地漫画库读取失败"
            retry = True
        elif self._items:
            text = "没有匹配的漫画"
            retry = False
        else:
            text = "本地漫画库为空"
            retry = False
        self.state_label.setText(text)
        self.retry_button.setVisible(retry)
        self.content_stack.setCurrentWidget(self.state_panel)

    def _set_loading(self, loading: bool) -> None:
        self._loading = bool(loading)
        self.loading_bar.setVisible(self._loading)
        self.refresh_button.setEnabled(
            self._controller is not None and not self._loading
        )
        self._sync_content_state()

    def _set_active_albums(self, album_ids) -> None:
        self._active_albums = frozenset(album_ids)
        self._sync_card_activity()

    def _set_busy_albums(self, album_ids) -> None:
        self._busy_albums = frozenset(album_ids)
        self._sync_card_activity()

    def _sync_card_activity(self) -> None:
        for album_id, row in self._rows.items():
            row.set_activity(
                album_id in self._active_albums,
                album_id in self._busy_albums,
            )
        self._selected_ids.difference_update(
            self._active_albums | self._busy_albums
        )
        self._sync_selection_controls()

    def _on_thumbnail_ready(self, album_id: str, revision: int, image) -> None:
        row = self._rows.get(album_id)
        current = self._preview_state.get(album_id)
        if row is None or current is None or current[1] != revision:
            return
        row.set_preview(image, revision)

    def _open_item(self, album_id: str, kind: str) -> None:
        if self._controller is not None:
            self._controller.open_item(album_id, kind)

    @Slot(str)
    def prepare_local_read(
        self,
        album_id: str,
        history_entry: ReaderHistoryEntry | None = None,
    ) -> None:
        if self._controller is None:
            self.local_read_failed.emit(
                history_entry,
                "本地章节检查服务不可用",
            )
            return
        request_id = self._controller.check_chapters(str(album_id))
        if request_id is None:
            self.local_read_failed.emit(
                history_entry,
                "本地章节检查未能启动，请稍后重试",
            )
            return
        self._local_read_requests[int(request_id)] = (
            str(album_id),
            history_entry,
        )

    def _open_chapter_action(self, album_id: str, action: str) -> None:
        item = next(
            (
                value
                for value in self._items
                if value.album_id == str(album_id)
            ),
            None,
        )
        if item is None:
            return
        if action == "identify":
            self._start_legacy_identification(item)
            return
        if action != "manage" or self._controller is None:
            return

        current = self._chapter_dialogs.get(item.album_id)
        if current is not None and isValid(current):
            current.show()
            current.raise_()
            current.activateWindow()
            return

        dialog = LibraryChapterDialog(
            item.album_id,
            item.title,
            self,
        )
        self._chapter_dialogs[item.album_id] = dialog
        dialog.destroyed.connect(
            lambda _object=None, current_id=item.album_id, current=dialog: (
                self._forget_chapter_dialog(current_id, current)
            )
        )
        dialog.recheck_requested.connect(
            lambda current=dialog: self._request_chapter_check(current)
        )
        dialog.rebuild_requested.connect(
            lambda photo_ids, current=dialog: (
                self._request_chapter_rebuild(current, photo_ids)
            )
        )
        dialog.repair_requested.connect(
            lambda photo_ids, current=dialog: (
                self._request_chapter_repair(current, photo_ids)
            )
        )
        dialog.delete_requested.connect(
            lambda snapshot, kind, current=dialog: (
                self._confirm_chapter_delete(current, snapshot, kind)
            )
        )
        dialog.show()
        self._request_chapter_check(dialog)

    def _forget_chapter_dialog(
        self,
        album_id: str,
        dialog: LibraryChapterDialog,
    ) -> None:
        if self._chapter_dialogs.get(album_id) is dialog:
            self._chapter_dialogs.pop(album_id, None)
        for request_id, context in tuple(self._chapter_requests.items()):
            if context[2] is dialog:
                self._chapter_requests.pop(request_id, None)

    def _dialog_is_current(
        self,
        album_id: str,
        dialog,
    ) -> bool:
        return (
            dialog is not None
            and isValid(dialog)
            and self._chapter_dialogs.get(album_id) is dialog
        )

    def _request_chapter_check(self, dialog: LibraryChapterDialog) -> None:
        if (
            self._controller is None
            or not self._dialog_is_current(dialog.album_id, dialog)
        ):
            return
        dialog.set_loading(True)
        request_id = self._controller.check_chapters(dialog.album_id)
        self._remember_chapter_request(
            request_id,
            "check_chapters",
            dialog.album_id,
            dialog,
        )

    def _request_chapter_rebuild(
        self,
        dialog: LibraryChapterDialog,
        photo_ids,
    ) -> None:
        if self._controller is None or not photo_ids:
            return
        selected = tuple(
            value
            for value in dialog.selected_snapshots()
            if (
                value.image_status is ChapterImageStatus.COMPLETE
                and (
                    value.package_format is None
                    or value.package_status
                    in {
                        ChapterPackageStatus.MISSING,
                        ChapterPackageStatus.DAMAGED,
                    }
                )
            )
        )
        if not selected:
            dialog.show_result(
                "所选章节中没有可从完整图片重建的项目。",
                warning=True,
            )
            return
        choices = self._confirm_unknown_formats(selected, dialog)
        if choices is None:
            return
        dialog.set_loading(True, "正在后台重建所选章节…")
        request_id = self._controller.rebuild_chapters(
            dialog.album_id,
            tuple(value.photo_id for value in selected),
            choices,
        )
        self._remember_chapter_request(
            request_id,
            "rebuild_chapters",
            dialog.album_id,
            dialog,
        )

    def _request_chapter_repair(
        self,
        dialog: LibraryChapterDialog,
        photo_ids,
    ) -> None:
        if self._controller is None or not photo_ids:
            return
        selected = dialog.selected_snapshots()
        choices = self._confirm_unknown_formats(selected, dialog)
        if choices is None:
            return
        dialog.set_loading(
            True,
            "正在后台规划修复；完整图片会本地重建，缺图章节会创建下载任务…",
        )
        request_id = self._controller.repair_chapters(
            dialog.album_id,
            tuple(value.photo_id for value in selected),
            choices,
            self._current_task_config(),
        )
        self._remember_chapter_request(
            request_id,
            "repair_chapters",
            dialog.album_id,
            dialog,
        )

    def _confirm_unknown_formats(
        self,
        snapshots,
        parent,
    ) -> dict[str, str] | None:
        snapshots = tuple(snapshots)
        inferred = {
            value.photo_id: value.suggested_package_format
            for value in snapshots
            if (
                value.package_format is None
                and value.suggested_package_format is not None
            )
        }
        unknown = tuple(
            value
            for value in snapshots
            if (
                value.package_format is None
                and value.suggested_package_format is None
            )
        )
        if not unknown:
            return inferred
        selected = PackageFormatConfirmationDialog.choose(
            unknown,
            self._current_task_config().package_format,
            parent,
        )
        if selected is None:
            return None
        return {
            **inferred,
            **{value.photo_id: selected for value in unknown},
        }

    def _current_task_config(self) -> TaskConfig:
        if self._settings_controller is not None:
            return self._settings_controller.settings.task_config()
        return TaskConfig()

    def _confirm_chapter_delete(
        self,
        dialog: LibraryChapterDialog,
        snapshot,
        kind: str,
    ) -> None:
        if self._controller is None:
            return
        labels = {
            "images": "图片",
            "package": "打包产物",
            "all": "全部内容及清单记录",
        }
        label = labels.get(kind)
        if label is None:
            return
        answer = QMessageBox.question(
            dialog,
            "删除章节内容",
            (
                f"确定删除《{snapshot.title}》（章节 {snapshot.index}）的"
                f"{label}吗？\n此操作无法撤销。"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        dialog.set_loading(True, f"正在删除章节{label}…")
        request_id = self._controller.delete_chapter(
            dialog.album_id,
            snapshot.photo_id,
            kind,
            snapshot,
        )
        self._remember_chapter_request(
            request_id,
            f"delete_chapter_{kind}",
            dialog.album_id,
            dialog,
        )

    def _remember_chapter_request(
        self,
        request_id,
        command: str,
        album_id: str,
        dialog,
    ) -> None:
        if request_id is None:
            if dialog is not None and isValid(dialog):
                dialog.show_result("操作未能启动。", warning=True)
            return
        self._chapter_requests[int(request_id)] = (
            str(command),
            str(album_id),
            dialog,
        )

    def _on_library_request_completed(
        self,
        request_id: int,
        command: str,
        album_id: str,
        result,
    ) -> None:
        local_context = self._local_read_requests.pop(request_id, None)
        if local_context is not None:
            expected_album, history_entry = local_context
            if command != "check_chapters" or album_id != expected_album:
                return
            complete = tuple(
                value
                for value in result
                if value.image_status is ChapterImageStatus.COMPLETE
            )
            item = next(
                (
                    value
                    for value in self._items
                    if value.album_id == album_id
                ),
                None,
            )
            if not complete or item is None:
                self.local_read_failed.emit(
                    history_entry,
                    "没有图片完整的本地章节，请先在章节管理中修复",
                )
                return
            catalog = ChapterCatalogSnapshot(
                album_id=album_id,
                title=item.title,
                chapters=tuple(
                    ChapterSnapshot(
                        photo_id=value.photo_id,
                        index=value.index,
                        title=value.title,
                        downloaded=True,
                    )
                    for value in complete
                ),
            )
            self.local_read_ready.emit(
                SearchResultSnapshot(
                    album_id=album_id,
                    title=item.title,
                    chapter_catalog=catalog,
                ),
                history_entry,
            )
            return
        context = self._chapter_requests.pop(request_id, None)
        if context is None:
            return
        expected_command, expected_album, dialog = context
        if command != expected_command or album_id != expected_album:
            return

        if command == "plan_legacy_migration":
            preview = LegacyMigrationPreviewDialog(result, self)
            if preview.exec() == preview.DialogCode.Accepted:
                next_id = self._controller.migrate_legacy_layout(result)
                self._remember_chapter_request(
                    next_id,
                    "migrate_legacy_layout",
                    album_id,
                    None,
                )
            return
        if command == "migrate_legacy_layout":
            QMessageBox.information(
                self,
                "章节迁移完成",
                "旧版图片目录已转换为受管章节布局。",
            )
            self.refresh()
            return
        if not self._dialog_is_current(album_id, dialog):
            return
        if command == "check_chapters":
            dialog.set_snapshots(result)
            completion = self._chapter_completion_messages.pop(
                album_id,
                None,
            )
            if completion is not None:
                dialog.show_result(completion[0], warning=completion[1])
            return
        if command.startswith("delete_chapter_"):
            if getattr(result, "album_removed", False):
                dialog.close()
                QMessageBox.information(
                    self,
                    "章节删除完成",
                    "最后一章已删除，漫画已从本地库移除。",
                )
                return
            self._chapter_completion_messages[album_id] = (
                "章节内容已删除，状态已重新检查。",
                False,
            )
            self._request_chapter_check(dialog)
            return
        if command == "rebuild_chapters":
            succeeded = len(result.succeeded)
            failures = tuple(result.failures)
            message = f"重建成功：{succeeded} 章"
            if failures:
                message += "\n" + "\n".join(
                    f"{value.title or value.photo_id}：{value.message}"
                    for value in failures
                )
            self._chapter_completion_messages[album_id] = (
                message,
                bool(failures),
            )
            self._request_chapter_check(dialog)
            return
        if command == "repair_chapters":
            rebuilt = (
                len(result.rebuild_result.succeeded)
                if result.rebuild_result is not None
                else 0
            )
            rebuild_failures = (
                tuple(result.rebuild_result.failures)
                if result.rebuild_result is not None
                else ()
            )
            lines = [
                f"本地重建成功：{rebuilt} 章",
                f"新建重新下载任务：{len(result.created_tasks)} 个",
                f"无需处理：{len(result.plan.unchanged_photo_ids)} 章",
            ]
            failures = (*result.plan.failures, *rebuild_failures)
            if failures:
                lines.append("未完成：")
                lines.extend(
                    f"{value.title or value.photo_id}：{value.message}"
                    for value in failures
                )
            if result.task_error:
                lines.append(f"下载任务未创建：{result.task_error}")
            message = "\n".join(lines)
            warning = bool(failures or result.task_error)
            if result.created_tasks:
                dialog.show_result(
                    message
                    + "\n重新下载完成后，请点击“重新检查”更新本地状态。",
                    warning=warning,
                )
            else:
                self._chapter_completion_messages[album_id] = (
                    message,
                    warning,
                )
                self._request_chapter_check(dialog)

    def _on_library_request_failed(
        self,
        request_id: int,
        command: str,
        album_id: str,
        message: str,
    ) -> None:
        local_context = self._local_read_requests.pop(request_id, None)
        if local_context is not None:
            _expected_album, history_entry = local_context
            self.local_read_failed.emit(history_entry, message)
            return
        context = self._chapter_requests.pop(request_id, None)
        if context is None:
            return
        _expected_command, _expected_album, dialog = context
        if self._dialog_is_current(album_id, dialog):
            dialog.show_result(message, warning=True)
        elif command in {"plan_legacy_migration", "migrate_legacy_layout"}:
            QMessageBox.warning(self, "章节识别失败", message)

    def _start_legacy_identification(self, item: LibraryItem) -> None:
        if (
            self._chapter_catalog_controller is None
            or self._controller is None
        ):
            QMessageBox.warning(
                self,
                "无法识别章节",
                "远端章节目录服务不可用。",
            )
            return
        answer = QMessageBox.question(
            self,
            "识别旧版章节",
            (
                f"识别 JM {item.album_id} 的章节可能会访问网络读取远端目录。\n"
                "此步骤不会下载漫画，也不会在预览确认前修改本地文件。是否继续？"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        request_id = self._chapter_catalog_controller.request(item.album_id)
        if request_id is None:
            QMessageBox.warning(self, "无法识别章节", "远端目录请求未能启动。")
            return
        self._catalog_requests[int(request_id)] = item.album_id
        card = self._rows.get(item.album_id)
        if card is not None:
            card.chapter_button.setEnabled(False)
            card.chapter_button.setToolTip("正在读取远端章节目录")

    def _on_catalog_ready(self, request_id: int, catalog) -> None:
        album_id = self._catalog_requests.pop(request_id, None)
        if album_id is None or catalog.album_id != album_id:
            return
        self._restore_chapter_card(album_id)
        plan_id = self._controller.plan_legacy_migration(album_id, catalog)
        self._remember_chapter_request(
            plan_id,
            "plan_legacy_migration",
            album_id,
            None,
        )

    def _on_catalog_failed(
        self,
        request_id: int,
        _error_code: str,
        message: str,
    ) -> None:
        album_id = self._catalog_requests.pop(request_id, None)
        if album_id is None:
            return
        self._restore_chapter_card(album_id)
        QMessageBox.warning(self, "远端章节目录读取失败", message)

    def _restore_chapter_card(self, album_id: str) -> None:
        card = self._rows.get(album_id)
        if card is None:
            return
        card.set_activity(
            album_id in self._active_albums,
            album_id in self._busy_albums,
        )
        card.chapter_button.setToolTip("识别章节（可能访问网络）")

    def _confirm_delete(self, album_id: str, kind: str) -> None:
        if self._controller is None:
            return
        labels = {
            "images": "全部图片",
            "pdf": "全部打包产物（PDF 与 CBZ）",
            "all": "全部图片和打包产物",
        }
        target = labels.get(kind)
        if target is None:
            return
        answer = QMessageBox.question(
            self,
            "删除本地文件",
            f"确定删除 JM {album_id} 的{target}吗？此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._controller.delete_item(album_id, kind)

    def _confirm_batch_delete(self, kind: str) -> None:
        if self._controller is None or not self._selected_ids:
            return
        labels = {
            "images": "全部图片",
            "pdf": "全部打包产物（PDF 与 CBZ）",
            "all": "全部图片和打包产物",
        }
        target = labels.get(kind)
        if target is None:
            return
        album_ids = tuple(
            album_id
            for album_id in self._visible_ids
            if album_id in self._selected_ids
        )
        album_ids += tuple(
            sorted(
                self._selected_ids.difference(album_ids),
                key=int,
            )
        )
        answer = QMessageBox.question(
            self,
            "批量删除本地文件",
            (
                f"确定删除所选 {len(album_ids)} 本漫画的{target}吗？\n"
                "程序会逐本处理；单本失败不会中断后续项目。此操作无法撤销。"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_selected_button.setEnabled(False)
        if not self._controller.batch_delete(album_ids, kind):
            self._sync_selection_controls()

    def _on_batch_delete_finished(
        self,
        _kind: str,
        succeeded,
        failures,
    ) -> None:
        succeeded = tuple(str(value) for value in succeeded)
        failures = tuple(
            (str(album_id), str(message))
            for album_id, message in failures
        )
        self._selected_ids.difference_update(succeeded)
        for album_id in succeeded:
            row = self._rows.get(album_id)
            if row is not None:
                row.set_selected(False)
        self._sync_selection_controls()

        lines = [f"成功：{len(succeeded)} 本"]
        if failures:
            lines.append(f"失败：{len(failures)} 本")
            lines.extend(
                f"JM {album_id}：{message}"
                for album_id, message in failures
            )
            QMessageBox.warning(
                self,
                "批量删除完成（部分失败）",
                "\n".join(lines),
            )
        else:
            QMessageBox.information(
                self,
                "批量删除完成",
                "\n".join(lines),
            )

    def _show_command_error(
        self,
        command: str,
        _album_id: str,
        message: str,
    ) -> None:
        if command == "refresh":
            self._scan_error = message
            self.error_label.setText(message)
            self.error_banner.setVisible(bool(self._items))
            self._sync_content_state()
            return
        if command in {
            "check_chapters",
            "rebuild_chapters",
            "repair_chapters",
            "delete_chapter_images",
            "delete_chapter_package",
            "delete_chapter_all",
            "plan_legacy_migration",
            "migrate_legacy_layout",
        }:
            return
        QMessageBox.warning(self, "操作失败", message)
