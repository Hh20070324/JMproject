from html import escape

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ...models import SearchResultSnapshot


def _safe_tooltip_html(text: str) -> str:
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = escape(normalized, quote=True).replace("\n", "<br>")
    return f"<qt>{escaped}</qt>"


class ElidedTextLabel(QLabel):
    def __init__(self, maximum_lines: int = 1, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self._maximum_lines = max(1, int(maximum_lines))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )

    @property
    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(_safe_tooltip_html(self._full_text))
        self._update_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ):
            self._update_elision()

    def _update_elision(self) -> None:
        width = max(0, self.contentsRect().width())
        if not self._full_text or width <= 0:
            self.setText("")
            return

        if self._maximum_lines == 1:
            self.setText(
                self.fontMetrics().elidedText(
                    self._full_text,
                    Qt.TextElideMode.ElideRight,
                    width,
                )
            )
            return

        layout = QTextLayout(self._full_text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        lines = []
        layout.beginLayout()
        for _ in range(self._maximum_lines):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            lines.append(line)
        has_more = layout.createLine().isValid()
        layout.endLayout()

        if not lines:
            self.setText("")
            return

        rendered = []
        for index, line in enumerate(lines):
            start = line.textStart()
            if index == len(lines) - 1 and has_more:
                value = self._full_text[start:]
                value = self.fontMetrics().elidedText(
                    value,
                    Qt.TextElideMode.ElideRight,
                    width,
                )
            else:
                value = self._full_text[start : start + line.textLength()]
            rendered.append(value.rstrip())
        self.setText("\n".join(rendered))


class SearchResultCard(QFrame):
    WIDTH = 184
    HEIGHT = 348
    COVER_HEIGHT = 182

    download_requested = Signal(str)
    view_task_requested = Signal(str)

    def __init__(self, snapshot: SearchResultSnapshot, parent=None):
        super().__init__(parent)
        self.setObjectName("comicCard")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.snapshot = snapshot
        self._task_present = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.cover_label = QLabel(self)
        self.cover_label.setObjectName("coverPlaceholder")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setFixedHeight(self.COVER_HEIGHT)
        self.cover_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout.addWidget(self.cover_label)

        info = QWidget(self)
        info.setObjectName("comicInfo")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(10, 8, 10, 8)
        info_layout.setSpacing(3)

        self.title_label = ElidedTextLabel(2, info)
        self.title_label.setObjectName("comicTitle")
        self.title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.title_label.setFixedHeight(38)
        info_layout.addWidget(self.title_label)

        self.author_label = ElidedTextLabel(1, info)
        self.author_label.setObjectName("comicAuthor")
        self.author_label.setFixedHeight(20)
        info_layout.addWidget(self.author_label)

        self.album_id_label = ElidedTextLabel(1, info)
        self.album_id_label.setObjectName("comicMetadata")
        self.album_id_label.setFixedHeight(20)
        info_layout.addWidget(self.album_id_label)

        self.tags_label = ElidedTextLabel(1, info)
        self.tags_label.setObjectName("comicMetadata")
        self.tags_label.setFixedHeight(20)
        info_layout.addWidget(self.tags_label)
        info_layout.addStretch(1)

        self.action_button = QPushButton(info)
        self.action_button.setObjectName("searchResultActionButton")
        self.action_button.setFixedHeight(32)
        self.action_button.clicked.connect(self._emit_action)
        info_layout.addWidget(self.action_button)
        layout.addWidget(info, 1)

        self.clear_cover()
        self.update_snapshot(snapshot)
        self.set_task_present(False)

    @property
    def task_present(self) -> bool:
        return self._task_present

    def update_snapshot(self, snapshot: SearchResultSnapshot) -> None:
        self.snapshot = snapshot
        album_id = snapshot.album_id
        self.title_label.set_full_text(snapshot.title or f"JM {album_id}")
        self.author_label.set_full_text(
            f"作者：{' / '.join(snapshot.authors)}"
            if snapshot.authors
            else "作者：未知"
        )
        self.album_id_label.set_full_text(f"JM {album_id}")
        self.tags_label.set_full_text(
            f"标签：{' · '.join(snapshot.tags)}" if snapshot.tags else ""
        )

    def set_task_present(self, present: bool) -> None:
        self._task_present = bool(present)
        if self._task_present:
            self.action_button.setText("查看任务")
            self.action_button.setToolTip("查看此漫画的下载任务")
            icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_ArrowForward
            )
        else:
            self.action_button.setText("下载整本")
            self.action_button.setToolTip("将此漫画加入下载任务")
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        self.action_button.setIcon(icon)

    def set_cover(self, image: QImage) -> None:
        if image.isNull():
            self.clear_cover()
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self.cover_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_label.setPixmap(pixmap)
        self.cover_label.setText("")

    def clear_cover(self) -> None:
        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("JM")

    def _emit_action(self) -> None:
        if self._task_present:
            self.view_task_requested.emit(self.snapshot.album_id)
        else:
            self.download_requested.emit(self.snapshot.album_id)


__all__ = ["SearchResultCard"]
