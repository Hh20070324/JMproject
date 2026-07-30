from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import LibraryItem, LibraryLayout
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
    read_requested = Signal(str)
    open_requested = Signal(str, str)
    view_task_requested = Signal(str)
    delete_requested = Signal(str, str)
    chapter_action_requested = Signal(str, str)
    selection_changed = Signal(str, bool)

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
        self._selection_mode = False

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
        self.selection_checkbox = QCheckBox(details)
        self.selection_checkbox.setObjectName("librarySelectionCheck")
        self.selection_checkbox.setToolTip("选择这本漫画")
        self.selection_checkbox.toggled.connect(
            lambda checked: self._selection_toggled(checked)
        )
        self.selection_checkbox.hide()
        heading_layout.addWidget(self.selection_checkbox)
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

        secondary_actions = QWidget(actions)
        secondary_actions.setObjectName("librarySecondaryActions")
        secondary_layout = QHBoxLayout(secondary_actions)
        secondary_layout.setContentsMargins(0, 0, 0, 0)
        secondary_layout.setSpacing(5)

        self.view_task_button = self._make_icon_button(
            secondary_actions,
            "libraryViewTaskButton",
            "在下载任务中定位这本漫画",
            svg_icon("download"),
        )
        self.view_task_button.clicked.connect(
            lambda: self.view_task_requested.emit(self.item.album_id)
        )
        secondary_layout.addWidget(self.view_task_button)

        self.chapter_button = self._make_icon_button(
            secondary_actions,
            "libraryChapterButton",
            "管理章节",
            svg_icon("menu"),
        )
        self.chapter_button.clicked.connect(
            lambda: self.chapter_action_requested.emit(
                self.item.album_id,
                (
                    "identify"
                    if self.item.layout is LibraryLayout.LEGACY
                    else "manage"
                ),
            )
        )
        secondary_layout.addWidget(self.chapter_button)

        self.delete_button = self._make_icon_button(
            secondary_actions,
            "libraryDeleteButton",
            "删除本地文件",
            svg_icon("trash"),
        )
        self.delete_menu = QMenu(self.delete_button)
        self.delete_menu.setObjectName("libraryDeleteMenu")
        self.delete_images_action = QAction("删除全部图片", self.delete_menu)
        self.delete_pdf_action = QAction(
            "删除全部打包产物",
            self.delete_menu,
        )
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
        self.delete_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        secondary_layout.addWidget(self.delete_button)
        actions_layout.addWidget(secondary_actions)
        actions_layout.addStretch(1)

        primary_actions = QWidget(actions)
        primary_actions.setObjectName("libraryPrimaryActions")
        primary_layout = QHBoxLayout(primary_actions)
        primary_layout.setContentsMargins(0, 0, 0, 0)
        primary_layout.setSpacing(5)

        self.open_images_button = self._make_text_button(
            primary_actions,
            "libraryOpenImagesButton",
            "图片",
            "打开图片目录",
            svg_icon("folder"),
        )
        self.open_images_button.clicked.connect(
            lambda: self.open_requested.emit(self.item.album_id, "images")
        )
        primary_layout.addWidget(self.open_images_button)

        self.open_pdf_button = self._make_text_button(
            primary_actions,
            "libraryOpenPdfButton",
            "打包",
            "使用文件资源管理器打开本漫画打包产物文件夹",
            svg_icon("document"),
        )
        self.open_pdf_button.clicked.connect(
            lambda: self.open_requested.emit(self.item.album_id, "package")
        )
        primary_layout.addWidget(self.open_pdf_button)

        self.read_button = self._make_text_button(
            primary_actions,
            "libraryReadButton",
            "完整阅读",
            "在应用内阅读本地完整章节",
            svg_icon("book"),
        )
        self.read_button.clicked.connect(
            lambda: self.read_requested.emit(self.item.album_id)
        )
        primary_layout.addWidget(self.read_button)
        actions_layout.addWidget(primary_actions)
        details_layout.addWidget(actions)
        layout.addWidget(details, 1)
        self._action_layouts = (
            primary_layout,
            actions_layout,
            details_layout,
            layout,
        )

        self.update_item(item)

    @property
    def is_selected(self) -> bool:
        return self.selection_checkbox.isChecked()

    def set_selection_mode(self, enabled: bool) -> None:
        self._selection_mode = bool(enabled)
        self.selection_checkbox.setVisible(self._selection_mode)
        if not self._selection_mode:
            self.set_selected(False)
        self._sync_activity()

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected) and self._selection_mode
        if selected == self.selection_checkbox.isChecked():
            self._sync_selected_style()
            return
        blocker = QSignalBlocker(self.selection_checkbox)
        self.selection_checkbox.setChecked(selected)
        del blocker
        self._sync_selected_style()

    @staticmethod
    def _make_icon_button(parent, object_name, tooltip, icon) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setIcon(icon)
        button.setFixedSize(34, 34)
        return button

    @staticmethod
    def _make_text_button(
        parent,
        object_name,
        text,
        tooltip,
        icon,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(object_name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setIcon(icon)
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        button.setFixedHeight(34)
        button.setSizePolicy(
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Fixed,
        )
        return button

    def update_item(self, item: LibraryItem) -> None:
        self.item = item
        heading = (
            f"{item.title} · JM {item.album_id}"
            if item.title
            else f"JM {item.album_id}"
        )
        self.album_id_label.setText(heading)
        self.album_id_label.setToolTip(heading)
        self.image_meta.setText(
            f"图片 {item.image_count} 张 · {format_file_size(item.image_size)}"
            if item.has_images
            else "没有本地图片"
        )
        if item.layout is LibraryLayout.LEGACY:
            self.chapter_meta.setText(
                f"章节信息未知 · 目录 {item.chapter_count} 个"
            )
        elif item.layout is LibraryLayout.UNVERIFIED:
            self.chapter_meta.setText("章节信息未知")
        else:
            self.chapter_meta.setText(f"章节 {item.chapter_count} 个")
        package_labels = []
        if item.has_pdf:
            package_labels.append(
                f"PDF {format_file_size(item.pdf_size)}"
            )
        if item.has_cbz:
            package_labels.append(
                f"CBZ {format_file_size(item.cbz_size)}"
            )
        self.pdf_meta.setText(
            "打包 · " + " / ".join(package_labels)
            if package_labels
            else "打包 · 未生成"
        )
        self.open_images_button.setVisible(item.has_images)
        self.read_button.setVisible(
            item.layout is LibraryLayout.MANAGED and item.has_images
        )
        self.open_pdf_button.setVisible(item.has_pdf or item.has_cbz)
        self.open_pdf_button.setEnabled(
            (
                item.pdf_directory is not None
                and item.pdf_directory.is_dir()
            )
            or (
                item.cbz_directory is not None
                and item.cbz_directory.is_dir()
            )
        )
        self.delete_images_action.setVisible(item.has_images)
        self.delete_pdf_action.setVisible(item.has_pdf or item.has_cbz)
        self.chapter_button.setVisible(
            item.layout in {LibraryLayout.MANAGED, LibraryLayout.LEGACY}
        )
        self.chapter_button.setToolTip(
            "识别章节（可能访问网络）"
            if item.layout is LibraryLayout.LEGACY
            else "管理章节"
        )
        self.chapter_button.setIcon(
            svg_icon(
                "scan"
                if item.layout is LibraryLayout.LEGACY
                else "menu"
            )
        )
        for action_layout in self._action_layouts:
            action_layout.invalidate()
        self.updateGeometry()
        self._sync_activity()
        if not item.has_images:
            self.reset_preview()

    def set_activity(self, active: bool, busy: bool) -> None:
        self._active = bool(active)
        self._busy = bool(busy)
        self._sync_activity()

    def _sync_activity(self) -> None:
        locked = self._active or self._busy
        if locked and self.selection_checkbox.isChecked():
            self.selection_checkbox.setChecked(False)
        self.selection_checkbox.setEnabled(
            self._selection_mode and not locked
        )
        self.delete_button.setEnabled(not locked)
        self.read_button.setEnabled(not self._busy)
        self.chapter_button.setEnabled(not locked)
        self.delete_images_action.setEnabled(not locked and self.item.has_images)
        self.delete_pdf_action.setEnabled(
            not locked and (self.item.has_pdf or self.item.has_cbz)
        )
        self.delete_all_action.setEnabled(not locked)

        if self._busy:
            self.state_label.setText("处理中")
            self.state_label.setProperty("state", "busy")
            tooltip = "本地库操作正在进行"
        elif self._active:
            self.state_label.setText("任务占用")
            self.state_label.setProperty("state", "active")
            tooltip = "该漫画仍有下载任务，暂不可修改本地文件"
        elif self.item.layout is LibraryLayout.LEGACY:
            self.state_label.setText("旧版布局")
            self.state_label.setProperty("state", "legacy")
            tooltip = "旧版布局，章节信息未知"
        else:
            self.state_label.clear()
            self.state_label.setProperty("state", "")
            tooltip = ""
        self.state_label.setVisible(
            locked or self.item.layout is LibraryLayout.LEGACY
        )
        self.delete_button.setToolTip(tooltip or "删除本地文件")
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        self._sync_selected_style()

    def _selection_toggled(self, checked: bool) -> None:
        if checked and (self._active or self._busy):
            self.set_selected(False)
            return
        self._sync_selected_style()
        self.selection_changed.emit(self.item.album_id, bool(checked))

    def _sync_selected_style(self) -> None:
        self.setProperty(
            "selected",
            self._selection_mode and self.selection_checkbox.isChecked(),
        )
        self.style().unpolish(self)
        self.style().polish(self)

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
