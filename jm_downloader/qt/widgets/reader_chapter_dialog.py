from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...models import ChapterCatalogSnapshot


class ReaderChapterDialog(QDialog):
    def __init__(
        self,
        catalog: ChapterCatalogSnapshot,
        *,
        current_photo_id: str | None = None,
        parent=None,
    ):
        if not isinstance(catalog, ChapterCatalogSnapshot):
            raise TypeError("catalog must be ChapterCatalogSnapshot")
        super().__init__(parent)
        self.catalog = catalog
        self._selected_photo_id = None
        self.setObjectName("readerChapterDialog")
        self.setWindowTitle("选择阅读章节")
        self.setModal(True)
        self.setMinimumSize(400, 320)
        self.resize(520, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel(catalog.title or f"JM {catalog.album_id}", self)
        title.setObjectName("readerChapterDialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(title)

        detail = QLabel(
            "选择一次即切换章节，不会创建下载任务",
            self,
        )
        detail.setObjectName("readerChapterDialogDetail")
        root.addWidget(detail)

        scroll = QScrollArea(self)
        scroll.setObjectName("readerChapterScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget(scroll)
        content.setObjectName("readerChapterCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        buttons = []
        for chapter in catalog.chapters:
            button = QRadioButton(
                f"第 {chapter.index} 章 · {chapter.title}",
                content,
            )
            button.setObjectName("readerChapterOption")
            button.setProperty("photo_id", chapter.photo_id)
            button.setChecked(chapter.photo_id == current_photo_id)
            button.clicked.connect(
                lambda _checked=False, photo_id=chapter.photo_id: (
                    self._choose(photo_id)
                )
            )
            layout.addWidget(button)
            buttons.append(button)
        layout.addStretch(1)
        self.chapter_buttons = tuple(buttons)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("取消", self)
        cancel.setObjectName("readerChapterCancelButton")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        root.addLayout(footer)

    def selected_photo_id(self) -> str | None:
        return self._selected_photo_id

    def _choose(self, photo_id: str) -> None:
        self._selected_photo_id = photo_id
        self.accept()


__all__ = ["ReaderChapterDialog"]
