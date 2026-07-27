from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ...models import ReaderHistoryEntry
from ...reader import ReaderHistoryStore


class ReaderHistoryDialog(QDialog):
    def __init__(
        self,
        store: ReaderHistoryStore,
        parent=None,
    ):
        super().__init__(parent)
        if not isinstance(store, ReaderHistoryStore):
            raise TypeError("store must be ReaderHistoryStore")
        self.store = store
        self._entries: dict[str, ReaderHistoryEntry] = {}
        self._selected_entry: ReaderHistoryEntry | None = None
        self.setObjectName("readerHistoryDialog")
        self.setWindowTitle("阅读历史")
        self.setModal(True)
        self.resize(620, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("阅读历史", self)
        title.setObjectName("readerHistoryDialogTitle")
        root.addWidget(title)
        detail = QLabel(
            "最多保留 100 部漫画。打开列表不会联网或预取章节。",
            self,
        )
        detail.setObjectName("readerHistoryDialogDetail")
        detail.setWordWrap(True)
        root.addWidget(detail)

        self.list_widget = QListWidget(self)
        self.list_widget.setObjectName("readerHistoryList")
        self.list_widget.setAlternatingRowColors(False)
        self.list_widget.itemSelectionChanged.connect(
            self._refresh_buttons
        )
        self.list_widget.itemDoubleClicked.connect(
            lambda _item: self._continue_selected()
        )
        root.addWidget(self.list_widget, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.clear_button = QPushButton("清空全部", self)
        self.clear_button.setObjectName("readerHistoryClearButton")
        self.clear_button.clicked.connect(self._clear_all)
        actions.addWidget(self.clear_button)
        self.delete_button = QPushButton("删除所选", self)
        self.delete_button.setObjectName("readerHistoryDeleteButton")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        cancel = QPushButton("取消", self)
        cancel.setObjectName("readerHistoryCancelButton")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.continue_button = QPushButton("继续阅读", self)
        self.continue_button.setObjectName("readerHistoryContinueButton")
        self.continue_button.clicked.connect(self._continue_selected)
        actions.addWidget(self.continue_button)
        root.addLayout(actions)

        self._reload()

    def selected_entry(self) -> ReaderHistoryEntry | None:
        return self._selected_entry

    def _reload(self) -> None:
        entries = self.store.load()
        self._entries = {entry.album_id: entry for entry in entries}
        self.list_widget.clear()
        for entry in entries:
            item = QListWidgetItem(self._entry_text(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry.album_id)
            item.setToolTip(
                f"{entry.title}\nJM {entry.album_id}\n"
                f"{entry.chapter_title} · 第 {entry.page_number} 页"
            )
            self.list_widget.addItem(item)
        if entries:
            self.list_widget.setCurrentRow(0)
        self._refresh_buttons()

    def _current_entry(self) -> ReaderHistoryEntry | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return self._entries.get(
            str(item.data(Qt.ItemDataRole.UserRole))
        )

    def _continue_selected(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self._selected_entry = entry
        self.accept()

    def _delete_selected(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self.store.remove(entry.album_id)
        self._reload()

    def _clear_all(self) -> None:
        if not self._entries:
            return
        answer = QMessageBox.question(
            self,
            "清空阅读历史",
            "确定清空全部阅读历史吗？此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.clear()
        self._reload()

    def _refresh_buttons(self) -> None:
        selected = self._current_entry() is not None
        self.delete_button.setEnabled(selected)
        self.continue_button.setEnabled(selected)
        self.clear_button.setEnabled(bool(self._entries))

    @staticmethod
    def _entry_text(entry: ReaderHistoryEntry) -> str:
        timestamp = entry.read_at_utc
        try:
            parsed = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
            timestamp = parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
        return (
            f"{entry.title}  ·  JM {entry.album_id}\n"
            f"第 {entry.chapter_index} 章 · {entry.chapter_title}"
            f"  ·  {entry.page_number} / {entry.page_count} 页"
            f"  ·  {timestamp}"
        )


__all__ = ["ReaderHistoryDialog"]
