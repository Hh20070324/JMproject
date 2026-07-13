from pathlib import Path
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
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import LibraryItem
from ..widgets.library_item_card import LibraryItemCard
from ..widgets.thumbnail_loader import ThumbnailLoader
from .base import SectionPage

if TYPE_CHECKING:
    from ..controllers.library_controller import LibraryController


class LibraryPage(SectionPage):
    FILTERS = (
        ("all", "全部"),
        ("images", "有图片"),
        ("pdf", "有 PDF"),
    )

    def __init__(
        self,
        controller: "LibraryController | None" = None,
        parent=None,
    ):
        super().__init__("本地漫画库", "libraryPage", parent)
        self._controller = controller
        self._items: list[LibraryItem] = []
        self._rows = {}
        self._preview_state = {}
        self._active_albums = frozenset()
        self._busy_albums = frozenset()
        self._visible_ids = ()
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
            self._items = self._controller.list_items()
            self._has_loaded = bool(self._items)
            self._active_albums = self._controller.active_album_ids()
            self._busy_albums = self._controller.busy_album_ids()
        else:
            self.search_input.setEnabled(False)
            for button in self._filter_buttons.values():
                button.setEnabled(False)
            self.refresh_button.setEnabled(False)

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
        self.search_input.setPlaceholderText("搜索 JM 号")
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
            button.setFixedSize(68, 34)
            button.clicked.connect(self._apply_filter)
            self._filter_group.addButton(button, index)
            self._filter_buttons[value] = button
            filter_layout.addWidget(button)
        self._filter_buttons["all"].setChecked(True)

        self.refresh_button = QToolButton(self.toolbar)
        self.refresh_button.setObjectName("refreshLibraryButton")
        self.refresh_button.setToolTip("刷新本地库")
        self.refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.refresh_button.setFixedSize(38, 38)
        self.refresh_button.clicked.connect(self.refresh)

        self.count_label = QLabel("0 / 0 本", self.toolbar)
        self.count_label.setObjectName("libraryCountLabel")
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.count_label.setMinimumWidth(84)
        self.content_layout.addWidget(self.toolbar)

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

    def _reflow_toolbar(self) -> None:
        compact = self.content.width() < 700
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        if compact:
            self.toolbar.setFixedHeight(88)
            self.toolbar_grid.addWidget(self.search_input, 0, 0, 1, 3)
            self.toolbar_grid.addWidget(self.count_label, 0, 3)
            self.toolbar_grid.addWidget(self.filter_segment, 1, 0, 1, 2)
            self.toolbar_grid.addWidget(self.refresh_button, 1, 3)
        else:
            self.toolbar.setFixedHeight(42)
            self.toolbar_grid.addWidget(self.search_input, 0, 0)
            self.toolbar_grid.addWidget(self.filter_segment, 0, 1)
            self.toolbar_grid.addWidget(self.refresh_button, 0, 2)
            self.toolbar_grid.addWidget(self.count_label, 0, 3)
        self.toolbar_grid.setColumnStretch(0, 1)
        self.toolbar_grid.setColumnStretch(1, 0)
        self.toolbar_grid.setColumnStretch(2, 0)
        self.toolbar_grid.setColumnStretch(3, 0)

    def _set_items(self, items) -> None:
        self._items = list(items)
        self._has_loaded = True
        self._scan_error = None
        self.error_banner.hide()
        self._sync_rows()
        self._apply_filter(force=True)

    def _sync_rows(self) -> None:
        item_ids = {item.album_id for item in self._items}
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
                row.rebuild_requested.connect(self._rebuild_pdf)
                row.delete_requested.connect(self._confirm_delete)
                self._rows[item.album_id] = row
            else:
                row.update_item(item)
            row.set_activity(
                item.album_id in self._active_albums,
                item.album_id in self._busy_albums,
            )
            self._queue_preview(item, row)

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
        query = self.search_input.text().strip().lower()
        if query.startswith("#"):
            query = query[1:].strip()
        if query.startswith("jm"):
            query = query[2:].strip()
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
            if (not query or query in item.album_id.lower())
            and (
                selected == "all"
                or (selected == "images" and item.has_images)
                or (selected == "pdf" and item.has_pdf)
            )
        ]
        self._visible_ids = tuple(item.album_id for item in visible)
        self.count_label.setText(f"{len(visible)} / {len(self._items)} 本")
        self._reflow_cards(force=True if force else False)
        self._sync_content_state()

    def _reflow_cards(self, force: bool = False) -> None:
        available = max(1, self.scroll_area.viewport().width() - 8)
        card_width = max(
            (row.minimumSizeHint().width() for row in self._rows.values()),
            default=340,
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

    def _on_thumbnail_ready(self, album_id: str, revision: int, image) -> None:
        row = self._rows.get(album_id)
        current = self._preview_state.get(album_id)
        if row is None or current is None or current[1] != revision:
            return
        row.set_preview(image, revision)

    def _open_item(self, album_id: str, kind: str) -> None:
        if self._controller is not None:
            self._controller.open_item(album_id, kind)

    def _rebuild_pdf(self, album_id: str) -> None:
        if self._controller is None:
            return
        item = next(
            (item for item in self._items if item.album_id == album_id),
            None,
        )
        if item is not None and item.has_pdf:
            answer = QMessageBox.question(
                self,
                "重新生成 PDF",
                f"现有 JM {album_id} PDF 将被替换，确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._controller.rebuild_pdf(album_id)

    def _confirm_delete(self, album_id: str, kind: str) -> None:
        if self._controller is None:
            return
        labels = {
            "images": "全部图片",
            "pdf": "PDF",
            "all": "全部图片和 PDF",
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
        QMessageBox.warning(self, "操作失败", message)
