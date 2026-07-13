from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from ..widgets.task_row import DownloadTaskRow
from ..widgets.thumbnail_loader import ThumbnailLoader
from .base import SectionPage

if TYPE_CHECKING:
    from ..controllers.download_controller import DownloadController


class ComicPlaceholderCard(QFrame):
    WIDTH = 184
    HEIGHT = 292

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.setObjectName("comicCard")
        self.setProperty("placeholderIndex", index)
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.cover = QFrame(self)
        self.cover.setObjectName("coverPlaceholder")
        self.cover.setFixedHeight(182)
        cover_layout = QVBoxLayout(self.cover)
        cover_layout.setContentsMargins(12, 12, 12, 12)
        cover_layout.addStretch(1)

        cover_mark = QLabel("JM", self.cover)
        cover_mark.setObjectName("coverMark")
        cover_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_layout.addWidget(cover_mark)

        cover_caption = QLabel("封面预览", self.cover)
        cover_caption.setObjectName("coverCaption")
        cover_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_layout.addWidget(cover_caption)
        cover_layout.addStretch(1)
        layout.addWidget(self.cover)

        info = QWidget(self)
        info.setObjectName("comicInfo")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(4)

        title = QLabel(f"漫画标题 {index:02d}", info)
        title.setObjectName("comicTitle")
        info_layout.addWidget(title)

        author = QLabel("作者名称", info)
        author.setObjectName("comicAuthor")
        info_layout.addWidget(author)

        metadata = QLabel("JM 000000  ·  # 标签", info)
        metadata.setObjectName("comicMetadata")
        info_layout.addWidget(metadata)
        info_layout.addStretch(1)
        layout.addWidget(info, 1)


class DownloadPage(SectionPage):
    def __init__(self, controller: "DownloadController | None" = None, parent=None):
        super().__init__("下载任务", "downloadPage", parent)
        self._controller = controller
        self._task_rows = {}
        self._preview_requests = {}
        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.thumbnail_ready.connect(self._on_thumbnail_ready)

        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(12)

        self.general_search_input = QLineEdit(self.content)
        self.general_search_input.setObjectName("generalSearchInput")
        self.general_search_input.setPlaceholderText("搜索漫画名、标签或作者")
        self.general_search_input.setClearButtonEnabled(True)
        self.general_search_input.setFixedHeight(42)
        search_layout.addWidget(self.general_search_input, 1)

        self.jm_id_search_input = QLineEdit(self.content)
        self.jm_id_search_input.setObjectName("jmIdSearchInput")
        self.jm_id_search_input.setPlaceholderText("精确 JM 号")
        self.jm_id_search_input.setClearButtonEnabled(True)
        self.jm_id_search_input.setFixedSize(190, 42)
        search_layout.addWidget(self.jm_id_search_input)
        self.content_layout.addLayout(search_layout)

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
        self.view_tabs.currentChanged.connect(self.view_stack.setCurrentIndex)

        self._create_results_view()
        self._create_tasks_view()
        self.view_tabs.setCurrentIndex(0)

        if self._controller is not None:
            self._controller.tasks_reset.connect(self._set_tasks)
            self._controller.command_failed.connect(self._show_command_error)
            self._set_tasks(self._controller.list_tasks())

        self._column_count = 0
        QTimer.singleShot(0, self._reflow_cards)

    def _create_results_view(self) -> None:
        results_view = QWidget(self.view_stack)
        results_view.setObjectName("searchResultsView")
        results_layout = QVBoxLayout(results_view)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)

        self.results_scroll = QScrollArea(results_view)
        self.results_scroll.setObjectName("resultsScroll")
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.results_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
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

        self.comic_cards = tuple(
            ComicPlaceholderCard(index, self.results_canvas)
            for index in range(1, 9)
        )
        self.results_scroll.setWidget(self.results_canvas)
        self.results_scroll.viewport().installEventFilter(self)
        results_layout.addWidget(self.results_scroll)
        self.view_stack.addWidget(results_view)

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

    @property
    def column_count(self) -> int:
        return self._column_count

    def eventFilter(self, watched, event):
        if (
            watched is self.results_scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._reflow_cards)
        return super().eventFilter(watched, event)

    def _reflow_cards(self) -> None:
        available_width = max(1, self.results_scroll.viewport().width() - 8)
        step = ComicPlaceholderCard.WIDTH + self.results_grid.horizontalSpacing()
        columns = max(1, (available_width + self.results_grid.horizontalSpacing()) // step)
        if columns == self._column_count and self.results_grid.count():
            return

        while self.results_grid.count():
            self.results_grid.takeAt(0)
        for index, card in enumerate(self.comic_cards):
            self.results_grid.addWidget(card, index // columns, index % columns)
        self._column_count = columns

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
        for task_id in tuple(self._task_rows):
            if task_id in task_ids:
                continue
            row = self._task_rows.pop(task_id)
            self._preview_requests.pop(task_id, None)
            self._thumbnail_loader.clear_task(task_id)
            row.hide()
            row.deleteLater()

        for snapshot in snapshots:
            row = self._task_rows.get(snapshot.id)
            if row is None:
                row = DownloadTaskRow(snapshot, self.tasks_canvas)
                row.retry_requested.connect(self._controller.retry_task)
                row.remove_requested.connect(self._controller.remove_task)
                row.open_requested.connect(self._controller.open_item)
                self._task_rows[snapshot.id] = row
            else:
                row.update_snapshot(snapshot)

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

    def _on_thumbnail_ready(self, task_id: str, revision: int, image) -> None:
        row = self._task_rows.get(task_id)
        if row is None or row.snapshot.preview_revision != revision:
            return
        row.set_preview(image, revision)

    def _show_command_error(self, _command: str, message: str) -> None:
        QMessageBox.warning(self, "操作失败", message)
