from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import LibraryItem
from ..icons import svg_icon


def format_file_size(size: int) -> str:
    value = max(0, int(size))
    units = ("B", "KB", "MB", "GB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


class LibraryItemCard(QFrame):
    open_requested = Signal(str, str)
    rebuild_requested = Signal(str)
    delete_requested = Signal(str, str)

    def __init__(self, item: LibraryItem, parent=None):
        super().__init__(parent)
        self.setObjectName("libraryItemCard")
        self.setMinimumWidth(300)
        self.setFixedHeight(164)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.item = item
        self._preview_revision = -1
        self._active = False
        self._busy = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.preview = QLabel(self)
        self.preview.setObjectName("libraryPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(96, 140)
        layout.addWidget(self.preview)

        details = QWidget(self)
        details.setObjectName("libraryDetails")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(5)

        heading_layout = QHBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(8)
        self.album_id_label = QLabel(details)
        self.album_id_label.setObjectName("libraryAlbumId")
        heading_layout.addWidget(self.album_id_label, 1)
        self.state_label = QLabel(details)
        self.state_label.setObjectName("libraryState")
        self.state_label.hide()
        heading_layout.addWidget(self.state_label)
        details_layout.addLayout(heading_layout)

        self.image_meta = QLabel(details)
        self.image_meta.setObjectName("libraryMeta")
        details_layout.addWidget(self.image_meta)

        self.chapter_meta = QLabel(details)
        self.chapter_meta.setObjectName("libraryMeta")
        details_layout.addWidget(self.chapter_meta)

        self.pdf_meta = QLabel(details)
        self.pdf_meta.setObjectName("libraryMeta")
        details_layout.addWidget(self.pdf_meta)
        details_layout.addStretch(1)

        actions = QWidget(details)
        actions.setObjectName("libraryActions")
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(5)

        self.open_images_button = self._make_icon_button(
            actions,
            "libraryOpenImagesButton",
            "打开图片目录",
            svg_icon("folder"),
        )
        self.open_images_button.clicked.connect(
            lambda: self.open_requested.emit(self.item.album_id, "images")
        )
        actions_layout.addWidget(self.open_images_button)

        self.open_pdf_button = self._make_icon_button(
            actions,
            "libraryOpenPdfButton",
            "使用系统默认程序查看 PDF",
            svg_icon("document"),
        )
        self.open_pdf_button.clicked.connect(
            lambda: self.open_requested.emit(self.item.album_id, "pdf")
        )
        actions_layout.addWidget(self.open_pdf_button)

        self.rebuild_button = QToolButton(actions)
        self.rebuild_button.setObjectName("libraryRebuildButton")
        self.rebuild_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.rebuild_button.setIcon(svg_icon("refresh"))
        self.rebuild_button.setFixedSize(96, 34)
        self.rebuild_button.clicked.connect(
            lambda: self.rebuild_requested.emit(self.item.album_id)
        )
        actions_layout.addWidget(self.rebuild_button)

        self.delete_button = self._make_icon_button(
            actions,
            "libraryDeleteButton",
            "删除本地文件",
            svg_icon("trash"),
        )
        self.delete_menu = QMenu(self.delete_button)
        self.delete_menu.setObjectName("libraryDeleteMenu")
        self.delete_images_action = QAction("删除图片", self.delete_menu)
        self.delete_pdf_action = QAction("删除 PDF", self.delete_menu)
        self.delete_all_action = QAction("删除全部", self.delete_menu)
        self.delete_images_action.triggered.connect(
            lambda: self.delete_requested.emit(self.item.album_id, "images")
        )
        self.delete_pdf_action.triggered.connect(
            lambda: self.delete_requested.emit(self.item.album_id, "pdf")
        )
        self.delete_all_action.triggered.connect(
            lambda: self.delete_requested.emit(self.item.album_id, "all")
        )
        self.delete_menu.addAction(self.delete_images_action)
        self.delete_menu.addAction(self.delete_pdf_action)
        self.delete_menu.addSeparator()
        self.delete_menu.addAction(self.delete_all_action)
        self.delete_button.setMenu(self.delete_menu)
        self.delete_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        actions_layout.addWidget(self.delete_button)
        actions_layout.addStretch(1)
        details_layout.addWidget(actions)
        layout.addWidget(details, 1)

        self.update_item(item)

    @staticmethod
    def _make_icon_button(parent, object_name, tooltip, icon) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setIcon(icon)
        button.setFixedSize(34, 34)
        return button

    def update_item(self, item: LibraryItem) -> None:
        self.item = item
        self.album_id_label.setText(f"JM {item.album_id}")
        self.image_meta.setText(
            f"图片 {item.image_count} 张 · {format_file_size(item.image_size)}"
            if item.has_images
            else "没有本地图片"
        )
        self.chapter_meta.setText(
            f"章节 {item.chapter_count} 个" if item.has_images else "章节 0 个"
        )
        self.pdf_meta.setText(
            f"PDF · {format_file_size(item.pdf_size)}"
            if item.has_pdf
            else "PDF · 未生成"
        )
        self.open_images_button.setVisible(item.has_images)
        self.open_pdf_button.setVisible(item.has_pdf)
        self.rebuild_button.setVisible(item.has_images)
        self.rebuild_button.setText("重建 PDF" if item.has_pdf else "生成 PDF")
        self.delete_images_action.setVisible(item.has_images)
        self.delete_pdf_action.setVisible(item.has_pdf)
        self._sync_activity()
        if not item.has_images:
            self.reset_preview()

    def set_activity(self, active: bool, busy: bool) -> None:
        self._active = bool(active)
        self._busy = bool(busy)
        self._sync_activity()

    def _sync_activity(self) -> None:
        locked = self._active or self._busy
        self.rebuild_button.setEnabled(not locked)
        self.delete_button.setEnabled(not locked)
        self.delete_images_action.setEnabled(not locked and self.item.has_images)
        self.delete_pdf_action.setEnabled(not locked and self.item.has_pdf)
        self.delete_all_action.setEnabled(not locked)

        if self._busy:
            self.state_label.setText("处理中")
            self.state_label.setProperty("state", "busy")
            tooltip = "本地库操作正在进行"
        elif self._active:
            self.state_label.setText("下载中")
            self.state_label.setProperty("state", "active")
            tooltip = "下载进行中，暂不可修改本地文件"
        else:
            self.state_label.clear()
            self.state_label.setProperty("state", "")
            tooltip = ""
        self.state_label.setVisible(locked)
        self.rebuild_button.setToolTip(tooltip or self.rebuild_button.text())
        self.delete_button.setToolTip(tooltip or "删除本地文件")
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    def reset_preview(self) -> None:
        self.preview.setPixmap(QPixmap())
        self.preview.setText("PDF" if self.item.has_pdf else "JM")
        self._preview_revision = -1

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
