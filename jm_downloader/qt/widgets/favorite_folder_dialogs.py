from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...models import FavoriteFolderSnapshot, FavoritesSnapshot
from ..icons import svg_icon


class FavoriteTargetDialog(QDialog):
    def __init__(
        self,
        folders: tuple[FavoriteFolderSnapshot, ...],
        parent=None,
        *,
        title: str = "选择收藏位置",
        description: str = "每本漫画只能选择一个收藏夹。",
    ):
        super().__init__(parent)
        self.setObjectName("favoriteTargetDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)
        self.setMaximumHeight(520)
        self._buttons = QButtonGroup(self)
        self._folder_ids: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(12)
        detail = QLabel(description, self)
        detail.setObjectName("favoriteDialogDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        scroll = QScrollArea(self)
        scroll.setObjectName("favoriteTargetScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        canvas = QWidget(scroll)
        choices = QVBoxLayout(canvas)
        choices.setContentsMargins(2, 2, 8, 2)
        choices.setSpacing(6)

        available = [
            folder for folder in folders if folder.folder_id != "0"
        ]
        options = [("0", "未分类（默认位置）", None)] + [
            (folder.folder_id, folder.name, len(folder.items))
            for folder in available
        ]
        for index, (folder_id, name, count) in enumerate(options):
            text = name if count is None else f"{name}（{count} 条）"
            button = QRadioButton(text, canvas)
            button.setObjectName("favoriteTargetOption")
            button.setMinimumHeight(34)
            self._buttons.addButton(button, index)
            self._folder_ids[index] = folder_id
            choices.addWidget(button)
            if index == 0:
                button.setChecked(True)
        choices.addStretch(1)
        scroll.setWidget(canvas)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.setObjectName("favoriteDialogButtons")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_folder_id(self) -> str:
        return self._folder_ids.get(self._buttons.checkedId(), "0")

    @classmethod
    def choose(
        cls,
        folders: tuple[FavoriteFolderSnapshot, ...],
        parent=None,
        *,
        title: str = "选择收藏位置",
        description: str = "每本漫画只能选择一个收藏夹。",
    ) -> str | None:
        dialog = cls(
            folders,
            parent,
            title=title,
            description=description,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_folder_id


class FavoriteFolderManagerDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("favoriteFolderManagerDialog")
        self.setWindowTitle("管理收藏夹")
        self.setModal(True)
        self.setMinimumSize(440, 420)
        self.controller = controller
        self._snapshot = controller.current_snapshot
        self._busy = bool(controller.is_busy)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(10)
        detail = QLabel(
            "只能删除空的自建收藏夹；“全部收藏”不会被修改。",
            self,
        )
        detail.setObjectName("favoriteDialogDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        self.folder_list = QListWidget(self)
        self.folder_list.setObjectName("favoriteFolderList")
        self.folder_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.folder_list.currentItemChanged.connect(self._render_controls)
        layout.addWidget(self.folder_list, 1)

        create_row = QHBoxLayout()
        create_row.setSpacing(8)
        self.name_input = QLineEdit(self)
        self.name_input.setObjectName("favoriteFolderNameInput")
        self.name_input.setPlaceholderText("新收藏夹名称")
        self.name_input.setClearButtonEnabled(True)
        self.name_input.setMaxLength(256)
        self.name_input.returnPressed.connect(self._create_folder)
        self.name_input.textChanged.connect(self._render_controls)
        create_row.addWidget(self.name_input, 1)
        self.create_button = QPushButton("新建", self)
        self.create_button.setObjectName("favoriteDialogPrimaryButton")
        self.create_button.setIcon(svg_icon("plus"))
        self.create_button.clicked.connect(self._create_folder)
        create_row.addWidget(self.create_button)
        layout.addLayout(create_row)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("favoriteDialogError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.delete_button = QPushButton("删除空收藏夹", self)
        self.delete_button.setObjectName("favoriteDialogDangerButton")
        self.delete_button.setIcon(svg_icon("trash"))
        self.delete_button.clicked.connect(self._confirm_delete)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        close_button = QPushButton("关闭", self)
        close_button.setObjectName("favoriteDialogSecondaryButton")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        controller.snapshot_changed.connect(self._on_snapshot)
        controller.busy_changed.connect(self._on_busy_changed)
        controller.mutation_failed.connect(self._on_mutation_failed)
        controller.mutation_refresh_failed.connect(
            self._on_mutation_refresh_failed
        )
        self._rebuild()

    def _rebuild(self) -> None:
        selected_id = self._selected_folder_id()
        self.folder_list.clear()
        folders = self._snapshot.folders if self._snapshot is not None else ()
        for folder in folders:
            label = f"{folder.name}    {len(folder.items)} 条"
            item = QListWidgetItem(label, self.folder_list)
            item.setData(Qt.ItemDataRole.UserRole, folder.folder_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, len(folder.items))
            if folder.folder_id == "0":
                item.setToolTip("全部收藏是只读汇总，不能删除")
            if folder.folder_id == selected_id:
                self.folder_list.setCurrentItem(item)
        if self.folder_list.currentItem() is None and self.folder_list.count():
            self.folder_list.setCurrentRow(0)
        self._render_controls()

    def _selected_folder_id(self) -> str | None:
        item = self.folder_list.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    @Slot()
    def _create_folder(self) -> None:
        name = " ".join(self.name_input.text().split())
        if not name or self._busy:
            return
        self._set_error("")
        if self.controller.create_folder(name) is not None:
            self.name_input.clear()

    @Slot()
    def _confirm_delete(self) -> None:
        item = self.folder_list.currentItem()
        if item is None or not self.delete_button.isEnabled():
            return
        folder_id = str(item.data(Qt.ItemDataRole.UserRole))
        name = item.text().rsplit("    ", 1)[0]
        answer = QMessageBox.question(
            self,
            "删除收藏夹",
            f"确定删除空收藏夹“{name}”吗？此操作会修改远端账号。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._set_error("")
            self.controller.delete_folder(folder_id)

    @Slot(object)
    def _on_snapshot(self, snapshot) -> None:
        if snapshot is not None and not isinstance(snapshot, FavoritesSnapshot):
            return
        self._snapshot = snapshot
        self._set_error("")
        self._rebuild()

    @Slot(bool, str)
    def _on_busy_changed(self, busy: bool, _command: str) -> None:
        self._busy = bool(busy)
        self._render_controls()

    @Slot(str, str, str)
    def _on_mutation_failed(
        self,
        _command: str,
        _code: str,
        message: str,
    ) -> None:
        self._set_error(message)

    @Slot(str, str, str)
    def _on_mutation_refresh_failed(
        self,
        _command: str,
        _code: str,
        message: str,
    ) -> None:
        self._set_error(message)

    @Slot()
    def _render_controls(self, *_args) -> None:
        item = self.folder_list.currentItem()
        folder_id = (
            None if item is None else item.data(Qt.ItemDataRole.UserRole)
        )
        count = (
            -1 if item is None else item.data(Qt.ItemDataRole.UserRole + 1)
        )
        self.name_input.setEnabled(not self._busy)
        self.create_button.setEnabled(
            not self._busy and bool(self.name_input.text().strip())
        )
        self.delete_button.setEnabled(
            not self._busy and folder_id not in {None, "0"} and count == 0
        )

    def _set_error(self, message: str) -> None:
        message = str(message).strip()
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))


__all__ = ["FavoriteFolderManagerDialog", "FavoriteTargetDialog"]
