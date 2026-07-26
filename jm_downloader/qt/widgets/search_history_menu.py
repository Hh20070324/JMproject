from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from ..icons import svg_icon


class SearchHistoryMenu(QMenu):
    entry_selected = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchHistoryMenu")
        self._entries = ()

    @property
    def entries(self) -> tuple:
        return self._entries

    def set_entries(self, entries) -> None:
        self.clear()
        self._entries = tuple(entries)
        for entry in self._entries:
            action = QWidgetAction(self)
            row = QWidget(self)
            row.setObjectName("searchHistoryRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(6, 2, 4, 2)
            layout.setSpacing(4)

            select = QToolButton(row)
            select.setObjectName("searchHistoryEntryButton")
            prefix = "JM" if entry.kind == "jm_id" else "关键词"
            select.setText(f"{prefix} · {entry.text}")
            select.setToolTip(entry.text)
            select.setFixedHeight(34)
            select.clicked.connect(
                lambda _checked=False, value=entry: (
                    self.close(),
                    self.entry_selected.emit(value),
                )
            )
            layout.addWidget(select, 1)

            delete = QToolButton(row)
            delete.setObjectName("searchHistoryDeleteButton")
            delete.setIcon(svg_icon("trash"))
            delete.setToolTip(f"删除历史：{entry.text}")
            delete.setFixedSize(30, 30)
            delete.clicked.connect(
                lambda _checked=False, value=entry: (
                    self.delete_requested.emit(value)
                )
            )
            layout.addWidget(delete)

            action.setDefaultWidget(row)
            self.addAction(action)


__all__ = ["SearchHistoryMenu"]
