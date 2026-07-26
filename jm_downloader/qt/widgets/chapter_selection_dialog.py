from html import escape

from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...models import MAX_CHAPTERS_PER_TASK, ChapterCatalogSnapshot
from ..icons import svg_icon


def _safe_tooltip_html(text: str) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    escaped = escape(normalized, quote=True).replace("\n", "<br>")
    return f"<qt>{escaped}</qt>"


class ElidedChapterCheckBox(QCheckBox):
    TEXT_MARGIN = 30

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._full_text = str(text)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setProperty("hovered", False)
        self.setToolTip(_safe_tooltip_html(self._full_text))
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self._update_elision()

    @property
    def full_text(self) -> str:
        return self._full_text

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

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._set_hovered(True)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._set_hovered(False)

    def _set_hovered(self, hovered: bool) -> None:
        if self.property("hovered") is hovered:
            return
        self.setProperty("hovered", hovered)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _update_elision(self) -> None:
        available = max(
            0,
            self.contentsRect().width() - self.TEXT_MARGIN,
        )
        if available <= 0:
            self.setText("")
            return
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available,
            )
        )


class SelectAllCheckBox(QCheckBox):
    """Display partial selection, but toggle all/none in one user action."""

    def nextCheckState(self) -> None:
        target = (
            Qt.CheckState.Unchecked
            if self.checkState() is Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self.setCheckState(target)


class ChapterSelectionDialog(QDialog):
    def __init__(
        self,
        catalog: ChapterCatalogSnapshot,
        parent=None,
    ):
        if not isinstance(catalog, ChapterCatalogSnapshot):
            raise TypeError("catalog must be ChapterCatalogSnapshot")
        super().__init__(parent)
        self.catalog = catalog
        self.setObjectName("chapterSelectionDialog")
        self.setWindowTitle("章节选择")
        self.setModal(True)
        self.setMinimumSize(400, 320)
        self.resize(520, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel(catalog.title or f"JM {catalog.album_id}", self)
        title.setObjectName("chapterDialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(title)

        metadata = QLabel(
            f"JM {catalog.album_id} · {len(catalog.chapters)} 个章节",
            self,
        )
        metadata.setObjectName("chapterDialogMetadata")
        root.addWidget(metadata)

        toolbar = QFrame(self)
        toolbar.setObjectName("chapterDialogToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 0, 10, 0)
        toolbar_layout.setSpacing(8)

        self.select_all_checkbox = SelectAllCheckBox("全选", toolbar)
        self.select_all_checkbox.setObjectName("chapterSelectAll")
        self.select_all_checkbox.setTristate(True)
        self.select_all_checkbox.checkStateChanged.connect(
            self._apply_select_all
        )
        toolbar_layout.addWidget(self.select_all_checkbox)

        self.include_downloaded_checkbox = QCheckBox(
            "包含已下载章节",
            toolbar,
        )
        self.include_downloaded_checkbox.setObjectName(
            "chapterIncludeDownloaded"
        )
        self.include_downloaded_checkbox.toggled.connect(
            self._set_include_downloaded
        )
        toolbar_layout.addWidget(self.include_downloaded_checkbox)
        toolbar_layout.addStretch(1)

        self.selection_summary = QLabel(toolbar)
        self.selection_summary.setObjectName("chapterSelectionSummary")
        toolbar_layout.addWidget(self.selection_summary)
        root.addWidget(toolbar)

        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("chapterValidationIssues")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        root.addWidget(self.validation_label)

        scroll = QScrollArea(self)
        scroll.setObjectName("chapterSelectionScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        content = QWidget(scroll)
        content.setObjectName("chapterSelectionContent")
        chapter_layout = QVBoxLayout(content)
        chapter_layout.setContentsMargins(8, 8, 8, 8)
        chapter_layout.setSpacing(4)

        checkboxes = []
        for chapter in catalog.chapters:
            downloaded_suffix = "（已下载）" if chapter.downloaded else ""
            text = (
                f"第 {chapter.index} 章 · {chapter.title}"
                f"{downloaded_suffix}"
            )
            checkbox = ElidedChapterCheckBox(text, content)
            checkbox.setObjectName("chapterItemCheck")
            checkbox.setProperty("chapter_id", chapter.photo_id)
            checkbox.setProperty("downloaded", chapter.downloaded)
            checkbox.setChecked(False)
            checkbox.setEnabled(not chapter.downloaded)
            checkbox.toggled.connect(self._on_chapter_toggled)
            chapter_layout.addWidget(checkbox)
            checkboxes.append(checkbox)
        chapter_layout.addStretch(1)
        self.chapter_checkboxes = tuple(checkboxes)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addStretch(1)

        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setObjectName("chapterCancelButton")
        self.cancel_button.setFixedHeight(36)
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)

        self.confirm_button = QPushButton(self)
        self.confirm_button.setObjectName("chapterConfirmButton")
        self.confirm_button.setFixedHeight(36)
        self.confirm_button.setIcon(svg_icon("download"))
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self.accept)
        footer.addWidget(self.confirm_button)
        root.addLayout(footer)

        self._update_selection_state()

    def selected_chapter_ids(self) -> tuple[str, ...]:
        return tuple(
            str(checkbox.property("chapter_id"))
            for checkbox in self.chapter_checkboxes
            if checkbox.isChecked()
        )

    def force_redownload_chapter_ids(self) -> tuple[str, ...]:
        return tuple(
            str(checkbox.property("chapter_id"))
            for checkbox in self.chapter_checkboxes
            if checkbox.isChecked()
            and bool(checkbox.property("downloaded"))
        )

    def set_validation_issues(self, issues) -> None:
        messages = tuple(
            str(getattr(issue, "message", issue)).strip()
            for issue in issues
            if str(getattr(issue, "message", issue)).strip()
        )
        if messages:
            self.validation_label.setText(
                "请调整选择后重试：\n" + "\n".join(messages)
            )
            self.validation_label.show()
        else:
            self.validation_label.clear()
            self.validation_label.hide()

    def _apply_select_all(self, state: Qt.CheckState) -> None:
        if state is Qt.CheckState.PartiallyChecked:
            return
        checked = state is Qt.CheckState.Checked
        for checkbox in self.chapter_checkboxes:
            blocker = QSignalBlocker(checkbox)
            checkbox.setChecked(checked and checkbox.isEnabled())
            del blocker
        self.set_validation_issues(())
        self._update_selection_state()

    def _on_chapter_toggled(self, _checked: bool) -> None:
        self.set_validation_issues(())
        self._update_selection_state()

    def _set_include_downloaded(self, include: bool) -> None:
        for checkbox in self.chapter_checkboxes:
            if not bool(checkbox.property("downloaded")):
                continue
            blocker = QSignalBlocker(checkbox)
            if not include:
                checkbox.setChecked(False)
            checkbox.setEnabled(include)
            del blocker
        self.set_validation_issues(())
        self._update_selection_state()

    def _update_selection_state(self) -> None:
        selected_count = sum(
            checkbox.isChecked() for checkbox in self.chapter_checkboxes
        )
        eligible_count = sum(
            checkbox.isEnabled() for checkbox in self.chapter_checkboxes
        )
        if selected_count == 0:
            state = Qt.CheckState.Unchecked
        elif eligible_count > 0 and selected_count == eligible_count:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        blocker = QSignalBlocker(self.select_all_checkbox)
        self.select_all_checkbox.setCheckState(state)
        del blocker

        task_count = (
            (selected_count + MAX_CHAPTERS_PER_TASK - 1)
            // MAX_CHAPTERS_PER_TASK
        )
        if eligible_count == 0 and not self.include_downloaded_checkbox.isChecked():
            summary = "全部章节已下载"
        else:
            summary = (
                f"已选 {selected_count} 章，将创建 {task_count} 个任务"
            )
        self.selection_summary.setText(summary)
        self.confirm_button.setText(f"确认下载（{selected_count}）")
        self.confirm_button.setEnabled(selected_count > 0)


__all__ = [
    "ChapterSelectionDialog",
    "ElidedChapterCheckBox",
    "SelectAllCheckBox",
]
