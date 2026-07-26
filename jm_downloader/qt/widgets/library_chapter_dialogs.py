from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import (
    ChapterImageStatus,
    ChapterPackageStatus,
    LegacyMigrationPlan,
    LibraryChapterSnapshot,
)


IMAGE_STATUS_LABELS = {
    ChapterImageStatus.COMPLETE: "完整",
    ChapterImageStatus.MISSING: "缺失",
    ChapterImageStatus.DAMAGED: "损坏",
}
PACKAGE_STATUS_LABELS = {
    ChapterPackageStatus.COMPLETE: "完整",
    ChapterPackageStatus.MISSING: "缺失",
    ChapterPackageStatus.DAMAGED: "损坏",
    ChapterPackageStatus.NOT_APPLICABLE: "无需打包",
    ChapterPackageStatus.UNKNOWN: "格式待确认",
}
FORMAT_LABELS = {
    "pdf": "PDF",
    "cbz": "CBZ",
    "images": "仅图片",
    None: "待确认",
}


class LibraryChapterDialog(QDialog):
    recheck_requested = Signal()
    repair_requested = Signal(object)
    rebuild_requested = Signal(object)
    delete_requested = Signal(object, str)

    def __init__(
        self,
        album_id: str,
        album_title: str | None,
        parent=None,
    ):
        super().__init__(parent)
        self.album_id = str(album_id)
        self.album_title = album_title or ""
        self._snapshots: tuple[LibraryChapterSnapshot, ...] = ()
        self._checks: dict[str, QCheckBox] = {}

        self.setObjectName("libraryChapterDialog")
        self.setWindowTitle("管理章节")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(960, 560)
        self.setMinimumSize(720, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(
            (
                f"{self.album_title} · JM {self.album_id}"
                if self.album_title
                else f"JM {self.album_id}"
            ),
            self,
        )
        title.setObjectName("libraryChapterDialogTitle")
        title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        title.setToolTip(title.text())
        layout.addWidget(title)

        self.summary_label = QLabel("正在离线检查章节…", self)
        self.summary_label.setObjectName("libraryChapterDialogSummary")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        toolbar = QWidget(self)
        toolbar.setObjectName("libraryChapterToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        self.select_all_button = QPushButton("全选可操作项", toolbar)
        self.select_all_button.setObjectName("libraryChapterSecondaryButton")
        self.select_all_button.clicked.connect(self._select_all)
        toolbar_layout.addWidget(self.select_all_button)
        toolbar_layout.addStretch(1)
        self.recheck_button = QPushButton("重新检查", toolbar)
        self.recheck_button.setObjectName("libraryChapterSecondaryButton")
        self.recheck_button.clicked.connect(self.recheck_requested.emit)
        toolbar_layout.addWidget(self.recheck_button)
        self.rebuild_button = QPushButton("重建所选", toolbar)
        self.rebuild_button.setObjectName("libraryChapterSecondaryButton")
        self.rebuild_button.clicked.connect(
            lambda: self.rebuild_requested.emit(self.selected_photo_ids())
        )
        toolbar_layout.addWidget(self.rebuild_button)
        self.repair_button = QPushButton("修复所选", toolbar)
        self.repair_button.setObjectName("libraryChapterPrimaryButton")
        self.repair_button.clicked.connect(
            lambda: self.repair_requested.emit(self.selected_photo_ids())
        )
        toolbar_layout.addWidget(self.repair_button)
        layout.addWidget(toolbar)

        self.table = QTableWidget(self)
        self.table.setObjectName("libraryChapterTable")
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            (
                "选择",
                "顺序",
                "章节",
                "图片",
                "原格式",
                "打包产物",
                "下载时间",
                "操作",
            )
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(52)
        layout.addWidget(self.table, 1)

        close_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        close_box.setObjectName("libraryChapterDialogButtons")
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self.set_loading(True)

    @property
    def snapshots(self) -> tuple[LibraryChapterSnapshot, ...]:
        return self._snapshots

    def set_loading(self, loading: bool, message: str | None = None) -> None:
        loading = bool(loading)
        self.table.setEnabled(not loading)
        self.select_all_button.setEnabled(not loading and bool(self._snapshots))
        self.recheck_button.setEnabled(not loading)
        self.rebuild_button.setEnabled(False if loading else bool(self.selected_photo_ids()))
        self.repair_button.setEnabled(False if loading else bool(self.selected_photo_ids()))
        if message:
            self.summary_label.setText(message)
        elif loading:
            self.summary_label.setText("正在离线检查章节，请稍候…")

    def set_snapshots(self, snapshots) -> None:
        values = tuple(
            value
            for value in snapshots
            if isinstance(value, LibraryChapterSnapshot)
            and value.album_id == self.album_id
        )
        selected = set(self.selected_photo_ids())
        self._snapshots = values
        self._checks.clear()
        self.table.setRowCount(len(values))
        for row, snapshot in enumerate(values):
            check = QCheckBox(self.table)
            check.setObjectName("libraryChapterCheck")
            check.setToolTip(f"选择章节：{snapshot.title}")
            check.setChecked(snapshot.photo_id in selected)
            check.toggled.connect(self._sync_buttons)
            check_container = QWidget(self.table)
            check_layout = QHBoxLayout(check_container)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check_layout.addWidget(check)
            self.table.setCellWidget(row, 0, check_container)
            self._checks[snapshot.photo_id] = check

            self._set_item(row, 1, str(snapshot.index), centered=True)
            self._set_item(row, 2, snapshot.title, tooltip=snapshot.title)
            self._set_item(
                row,
                3,
                (
                    f"{snapshot.valid_image_count}/{snapshot.page_count} "
                    f"{IMAGE_STATUS_LABELS[snapshot.image_status]}"
                ),
            )
            self._set_item(row, 4, FORMAT_LABELS[snapshot.package_format])
            self._set_item(
                row,
                5,
                PACKAGE_STATUS_LABELS[snapshot.package_status],
            )
            self._set_item(
                row,
                6,
                _format_downloaded_at(snapshot.downloaded_at_utc),
            )
            self.table.setCellWidget(row, 7, self._action_button(snapshot))

        issues = sum(bool(value.problem_codes) for value in values)
        if not values:
            self.summary_label.setText("当前清单没有章节记录。")
        elif issues:
            self.summary_label.setText(
                f"共 {len(values)} 章，其中 {issues} 章需要处理；检查过程未访问网络。"
            )
        else:
            self.summary_label.setText(
                f"共 {len(values)} 章，图片与打包状态均无异常；检查过程未访问网络。"
            )
        self.table.setEnabled(True)
        self.recheck_button.setEnabled(True)
        self.select_all_button.setEnabled(bool(values))
        self._sync_buttons()

    def selected_photo_ids(self) -> tuple[str, ...]:
        return tuple(
            snapshot.photo_id
            for snapshot in self._snapshots
            if (
                snapshot.photo_id in self._checks
                and self._checks[snapshot.photo_id].isChecked()
            )
        )

    def selected_snapshots(self) -> tuple[LibraryChapterSnapshot, ...]:
        selected = set(self.selected_photo_ids())
        return tuple(
            value for value in self._snapshots if value.photo_id in selected
        )

    def snapshot(self, photo_id: str) -> LibraryChapterSnapshot | None:
        return next(
            (
                value
                for value in self._snapshots
                if value.photo_id == str(photo_id)
            ),
            None,
        )

    def show_result(self, message: str, *, warning: bool = False) -> None:
        self.summary_label.setText(message)
        self.summary_label.setProperty("warning", bool(warning))
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)
        self.set_loading(False)

    def _set_item(
        self,
        row: int,
        column: int,
        text: str,
        *,
        tooltip: str | None = None,
        centered: bool = False,
    ) -> None:
        item = QTableWidgetItem(str(text))
        if tooltip:
            item.setToolTip(tooltip)
        if centered:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, column, item)

    def _action_button(
        self,
        snapshot: LibraryChapterSnapshot,
    ) -> QToolButton:
        button = QToolButton(self.table)
        button.setObjectName("libraryChapterActionButton")
        button.setText("删除 ▾")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        for kind, text, enabled in (
            ("images", "删除图片", snapshot.can_delete_images),
            ("package", "删除打包产物", snapshot.can_delete_package),
            ("all", "删除全部", snapshot.can_delete_all),
        ):
            action = QAction(text, menu)
            action.setEnabled(enabled)
            action.triggered.connect(
                lambda _checked=False, value=kind, current=snapshot: (
                    self.delete_requested.emit(current, value)
                )
            )
            menu.addAction(action)
        button.setMenu(menu)
        return button

    def _select_all(self) -> None:
        select = not all(check.isChecked() for check in self._checks.values())
        for check in self._checks.values():
            check.setChecked(select)

    def _sync_buttons(self) -> None:
        selected = self.selected_snapshots()
        self.repair_button.setEnabled(bool(selected))
        self.rebuild_button.setEnabled(
            bool(selected)
            and any(
                value.image_status is ChapterImageStatus.COMPLETE
                and (
                    value.package_format is None
                    or value.package_status
                    in {
                        ChapterPackageStatus.MISSING,
                        ChapterPackageStatus.DAMAGED,
                    }
                )
                for value in selected
            )
        )


class PackageFormatConfirmationDialog(QDialog):
    def __init__(
        self,
        snapshots,
        default_format: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("packageFormatConfirmationDialog")
        self.setWindowTitle("确认原打包格式")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        values = tuple(snapshots)
        detail = QLabel(
            (
                f"所选内容中有 {len(values)} 章无法从清单和磁盘确定原格式。\n"
                "下面的选择会写入这些章节的清单；当前设置只作为默认值。"
            ),
            self,
        )
        detail.setObjectName("libraryChapterDialogSummary")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        names = QLabel(
            "\n".join(
                f"第 {value.index} 章：{value.title}" for value in values
            ),
            self,
        )
        names.setObjectName("libraryChapterUnknownList")
        names.setWordWrap(True)
        names.setMaximumHeight(130)
        layout.addWidget(names)

        self.group = QButtonGroup(self)
        self.buttons = {}
        for index, (value, label) in enumerate(
            (("pdf", "PDF"), ("cbz", "CBZ"), ("images", "仅图片"))
        ):
            button = QRadioButton(label, self)
            button.setObjectName("libraryChapterFormatRadio")
            self.group.addButton(button, index)
            self.buttons[value] = button
            layout.addWidget(button)
        self.buttons.get(default_format, self.buttons["pdf"]).setChecked(True)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("确认")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    @classmethod
    def choose(
        cls,
        snapshots,
        default_format: str,
        parent=None,
    ) -> str | None:
        dialog = cls(snapshots, default_format, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return next(
            value
            for value, button in dialog.buttons.items()
            if button.isChecked()
        )


class LegacyMigrationPreviewDialog(QDialog):
    def __init__(self, plan: LegacyMigrationPlan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setObjectName("legacyMigrationPreviewDialog")
        self.setWindowTitle("确认旧版章节迁移")
        self.setModal(True)
        self.resize(700, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QLabel(
            f"{plan.album_title} · JM {plan.album_id}",
            self,
        )
        title.setObjectName("libraryChapterDialogTitle")
        layout.addWidget(title)
        detail = QLabel(
            "以下映射已经通过唯一性与路径安全检查。迁移只整理本地文件并生成章节清单，不会下载或打包。",
            self,
        )
        detail.setObjectName("libraryChapterDialogSummary")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        table = QTableWidget(len(plan.mappings), 4, self)
        table.setObjectName("legacyMigrationTable")
        table.setHorizontalHeaderLabels(("顺序", "本地来源", "远端章节", "新目录"))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.verticalHeader().hide()
        for row, mapping in enumerate(plan.mappings):
            table.setItem(row, 0, QTableWidgetItem(str(mapping.index)))
            table.setItem(
                row,
                1,
                QTableWidgetItem(mapping.source_name or "直接图片"),
            )
            table.setItem(row, 2, QTableWidgetItem(mapping.title))
            table.setItem(
                row,
                3,
                QTableWidgetItem(mapping.target_dir_name or "漫画标题目录"),
            )
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("开始迁移")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)


def _format_downloaded_at(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "未知"


__all__ = [
    "LegacyMigrationPreviewDialog",
    "LibraryChapterDialog",
    "PackageFormatConfirmationDialog",
]
