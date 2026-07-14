from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import TaskSnapshot, TaskStatus
from ..icons import svg_icon


class ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        available = max(0, self.contentsRect().width())
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available,
            )
        )


class DownloadTaskRow(QFrame):
    retry_requested = Signal(str)
    remove_requested = Signal(str)
    open_requested = Signal(str, str)

    STATUS_LABELS = {
        TaskStatus.PENDING: "等待中",
        TaskStatus.FETCHING: "读取信息",
        TaskStatus.DOWNLOADING: "下载中",
        TaskStatus.COMPLETED: "已完成",
        TaskStatus.FAILED: "失败",
    }

    def __init__(self, snapshot: TaskSnapshot, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadTaskRow")
        self.setFixedHeight(126)
        self.snapshot = snapshot
        self._preview_revision = -1

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.preview = QLabel("JM", self)
        self.preview.setObjectName("taskPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(72, 100)
        layout.addWidget(self.preview)

        main = QWidget(self)
        main.setObjectName("taskMain")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 1, 0, 1)
        main_layout.setSpacing(5)

        self.kicker = QLabel(main)
        self.kicker.setObjectName("taskKicker")
        main_layout.addWidget(self.kicker)

        self.title = ElidedLabel(main)
        self.title.setObjectName("taskTitle")
        main_layout.addWidget(self.title)

        self.progress = QProgressBar(main)
        self.progress.setObjectName("taskProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        main_layout.addWidget(self.progress)

        detail_layout = QHBoxLayout()
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        self.status = QLabel(main)
        self.status.setObjectName("taskStatus")
        detail_layout.addWidget(self.status)

        self.detail = ElidedLabel(main)
        self.detail.setObjectName("taskDetail")
        detail_layout.addWidget(self.detail, 1)
        main_layout.addLayout(detail_layout)
        layout.addWidget(main, 1)

        actions = QWidget(self)
        actions.setObjectName("taskActions")
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(5)

        self.retry_button = self._make_action(
            actions,
            "retryTaskButton",
            "重试下载",
            svg_icon("refresh"),
        )
        self.retry_button.clicked.connect(
            lambda: self.retry_requested.emit(self.snapshot.id)
        )
        actions_layout.addWidget(self.retry_button)

        self.open_images_button = self._make_action(
            actions,
            "openImagesButton",
            "打开图片目录",
            svg_icon("folder"),
        )
        self.open_images_button.clicked.connect(
            lambda: self.open_requested.emit(self.snapshot.album_id, "images")
        )
        actions_layout.addWidget(self.open_images_button)

        self.open_pdf_button = self._make_action(
            actions,
            "openPdfButton",
            "使用系统默认程序查看 PDF",
            svg_icon("document"),
        )
        self.open_pdf_button.clicked.connect(
            lambda: self.open_requested.emit(self.snapshot.album_id, "pdf")
        )
        actions_layout.addWidget(self.open_pdf_button)

        self.remove_button = self._make_action(
            actions,
            "removeTaskButton",
            "清除任务记录",
            svg_icon("trash"),
        )
        self.remove_button.clicked.connect(
            lambda: self.remove_requested.emit(self.snapshot.id)
        )
        actions_layout.addWidget(self.remove_button)
        layout.addWidget(actions)

        self.update_snapshot(snapshot)

    @staticmethod
    def _make_action(parent, object_name, tooltip, icon) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setIcon(icon)
        button.setFixedSize(34, 34)
        return button

    def update_snapshot(self, snapshot: TaskSnapshot) -> None:
        self.snapshot = snapshot
        status_value = snapshot.status.value
        self.setProperty("status", status_value)
        self.status.setProperty("status", status_value)
        self.style().unpolish(self)
        self.style().polish(self)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

        self.kicker.setText(f"JM {snapshot.album_id}")
        self.title.set_full_text(snapshot.title or "正在读取漫画信息")
        self.progress.setValue(snapshot.progress)
        self.progress.setVisible(snapshot.status != TaskStatus.FAILED)
        self.status.setText(self.STATUS_LABELS[snapshot.status])
        self.detail.set_full_text(self._detail_text(snapshot))

        terminal = snapshot.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        self.retry_button.setVisible(snapshot.status == TaskStatus.FAILED)
        self.remove_button.setVisible(snapshot.status == TaskStatus.PENDING or terminal)
        self.remove_button.setToolTip(
            "删除等待任务"
            if snapshot.status == TaskStatus.PENDING
            else "清除任务记录"
        )
        self.open_images_button.setVisible(terminal and snapshot.preview_path is not None)
        self.open_pdf_button.setVisible(terminal and snapshot.pdf_path is not None)

    def set_preview(self, image: QImage, revision: int) -> None:
        if image.isNull() or revision < self._preview_revision:
            return
        self._preview_revision = revision
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(pixmap)
        self.preview.setText("")

    @staticmethod
    def _detail_text(snapshot: TaskSnapshot) -> str:
        if snapshot.status == TaskStatus.FAILED:
            return snapshot.error or "未知错误"
        if snapshot.status == TaskStatus.COMPLETED:
            return "图片与 PDF 已保存到本地"
        if snapshot.status == TaskStatus.PENDING:
            return "等待空闲下载位置"
        if snapshot.status == TaskStatus.FETCHING:
            return "正在读取漫画信息"

        parts = [part for part in (snapshot.chapter, snapshot.page) if part]
        return " · ".join(parts) or "正在下载"
