from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .base import SectionPage


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
    def __init__(self, parent=None):
        super().__init__("下载任务", "downloadPage", parent)

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

        results_heading = QLabel("搜索结果", self.content)
        results_heading.setObjectName("sectionTitle")
        self.content_layout.addWidget(results_heading)

        self.results_scroll = QScrollArea(self.content)
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
        self.content_layout.addWidget(self.results_scroll, 1)

        self._column_count = 0
        QTimer.singleShot(0, self._reflow_cards)

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
