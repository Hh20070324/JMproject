from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import svg_icon


class SearchHistoryMenu(QFrame):
    """An in-page history panel that never takes focus from its editor."""

    entry_selected = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchHistoryMenu")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._entries = ()
        self._anchor = None
        self._event_filter_installed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("searchHistoryScroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content = QWidget(self.scroll)
        self.content.setObjectName("searchHistoryContent")
        self.content.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(6, 6, 6, 6)
        self.content_layout.setSpacing(3)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)
        self.hide()

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._event_filter_installed = True

    @property
    def entries(self) -> tuple:
        return self._entries

    def set_anchor(self, editor) -> None:
        self._anchor = editor

    def dispose(self) -> None:
        if self._event_filter_installed:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._event_filter_installed = False
        self._anchor = None
        self.close()

    def set_entries(self, entries) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._entries = tuple(entries)
        for entry in self._entries:
            row = QFrame(self.content)
            row.setObjectName("searchHistoryRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(3, 1, 3, 1)
            layout.setSpacing(4)

            select = QToolButton(row)
            select.setObjectName("searchHistoryEntryButton")
            select.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            prefix = "JM" if entry.kind == "jm_id" else "关键词"
            select.setText(f"{prefix} · {entry.text}")
            select.setToolTip(entry.text)
            select.setFixedHeight(34)
            select.clicked.connect(
                lambda _checked=False, value=entry: self._select_entry(
                    value
                )
            )
            layout.addWidget(select, 1)

            delete = QToolButton(row)
            delete.setObjectName("searchHistoryDeleteButton")
            delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            delete.setIcon(svg_icon("trash"))
            delete.setToolTip(f"删除历史：{entry.text}")
            delete.setFixedSize(30, 30)
            delete.clicked.connect(
                lambda _checked=False, value=entry: (
                    self.delete_requested.emit(value)
                )
            )
            layout.addWidget(delete)
            self.content_layout.addWidget(row)
        self.content_layout.addStretch(1)

    def popup(self, global_position: QPoint) -> None:
        parent = self.parentWidget()
        if parent is None or not self._entries:
            return
        row_height = 39
        height = min(320, len(self._entries) * row_height + 12)
        width = max(self.minimumWidth(), 260)
        local = parent.mapFromGlobal(global_position)
        x = max(0, min(local.x(), max(0, parent.width() - width)))
        y = local.y()
        if y + height > parent.height():
            anchor_height = (
                self._anchor.height()
                if self._anchor is not None
                else 0
            )
            y = max(0, y - height - anchor_height)
        self.setGeometry(x, y, width, height)
        self.show()
        self.raise_()

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type in (
            QEvent.Type.ApplicationDeactivate,
            QEvent.Type.WindowDeactivate,
        ) or (
            event_type == QEvent.Type.Hide
            and watched is self.parentWidget()
        ):
            self.close()
            return False
        if not self.isVisible():
            return False
        if (
            event_type == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.close()
            return False
        if event_type != QEvent.Type.MouseButtonPress:
            return False
        if watched is self._anchor:
            return False
        if not isinstance(watched, QWidget):
            global_position = getattr(event, "globalPosition", None)
            if callable(global_position):
                target = QApplication.widgetAt(
                    global_position().toPoint()
                )
                if target is self._anchor or (
                    target is not None
                    and (
                        target is self
                        or self.isAncestorOf(target)
                    )
                ):
                    return False
            else:
                return False
        elif watched is self or self.isAncestorOf(watched):
            return False
        self.close()
        return False

    def _select_entry(self, entry) -> None:
        self.entry_selected.emit(entry)
        self.close()


__all__ = ["SearchHistoryMenu"]
