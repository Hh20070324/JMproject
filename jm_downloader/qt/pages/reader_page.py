from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import (
    ChapterCatalogSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderSource,
)
from ...settings import READER_LAYOUT_MODES, READER_ZOOM_LEVELS
from ..controllers.reader_controller import ReaderController
from ..icons import svg_icon
from ..widgets.reader_chapter_dialog import ReaderChapterDialog
from ..widgets.reader_graphics_view import ReaderGraphicsView

if TYPE_CHECKING:
    from ..controllers.settings_controller import SettingsController


class ReaderPage(QWidget):
    back_requested = Signal()
    download_chapter_requested = Signal(str)
    chapter_selected = Signal(str)
    _LAYOUT_LABELS = {
        "fit_width": "适合宽度",
        "fit_page": "单页视图",
    }

    def __init__(
        self,
        controller: ReaderController,
        parent=None,
        *,
        settings_controller: "SettingsController | None" = None,
    ):
        super().__init__(parent)
        if not isinstance(controller, ReaderController):
            raise TypeError("controller must be ReaderController")
        self.controller = controller
        self.settings_controller = settings_controller
        self.setObjectName("readerPage")
        self._catalog: ChapterCatalogSnapshot | None = None
        self._chapter: ReaderChapterSnapshot | None = None
        self._source = ReaderSource.SEARCH
        self._album_title = ""
        self._generation = 0
        self._pending_photo_id: str | None = None
        self._pending_page = 1
        self._failed_pages: set[int] = set()
        self._layout_mode = (
            settings_controller.settings.reader_layout
            if settings_controller is not None
            else "fit_width"
        )
        self._zoom_percent = (
            settings_controller.settings.reader_zoom_percent
            if settings_controller is not None
            else 100
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.back_button = QToolButton(self)
        self.back_button.setObjectName("readerBackButton")
        self.back_button.setText("关闭阅读")
        self.back_button.setIcon(svg_icon("arrow-left"))
        self.back_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.back_button.clicked.connect(self._back)
        header.addWidget(self.back_button)
        self.title_label = QLabel("在线阅读", self)
        self.title_label.setObjectName("readerTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        header.addWidget(self.title_label, 1)
        root.addLayout(header)

        chapter_row = QHBoxLayout()
        chapter_row.setSpacing(8)
        self.chapter_button = QToolButton(self)
        self.chapter_button.setObjectName("readerChapterButton")
        self.chapter_button.setText("选择章节")
        self.chapter_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.chapter_button.clicked.connect(self._choose_chapter)
        chapter_row.addWidget(self.chapter_button, 1)
        self.previous_chapter_button = self._tool_button(
            "readerPreviousChapterButton",
            "上一章",
            "arrow-left",
            self._previous_chapter,
        )
        chapter_row.addWidget(self.previous_chapter_button)
        self.next_chapter_button = self._tool_button(
            "readerNextChapterButton",
            "下一章",
            "arrow-right",
            self._next_chapter,
        )
        chapter_row.addWidget(self.next_chapter_button)
        root.addLayout(chapter_row)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.previous_page_button = self._tool_button(
            "readerPreviousPageButton",
            "上一页",
            "arrow-left",
            self._previous_page,
        )
        progress_row.addWidget(self.previous_page_button)
        self.page_slider = QSlider(
            Qt.Orientation.Horizontal,
            self,
        )
        self.page_slider.setObjectName("readerPageSlider")
        self.page_slider.setTracking(False)
        self.page_slider.setRange(1, 1)
        self.page_slider.sliderMoved.connect(self._preview_slider)
        self.page_slider.sliderReleased.connect(self._apply_slider)
        progress_row.addWidget(self.page_slider, 1)
        self.page_label = QLabel("0 / 0", self)
        self.page_label.setObjectName("readerPageLabel")
        self.page_label.setMinimumWidth(76)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_row.addWidget(self.page_label)
        self.next_page_button = self._tool_button(
            "readerNextPageButton",
            "下一页",
            "arrow-right",
            self._next_page,
        )
        progress_row.addWidget(self.next_page_button)
        root.addLayout(progress_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.error_banner = QLabel(self)
        self.error_banner.setObjectName("readerErrorBanner")
        self.error_banner.setWordWrap(True)
        self.error_banner.hide()
        action_row.addWidget(self.error_banner, 1)
        self.layout_button = QToolButton(self)
        self.layout_button.setObjectName("readerLayoutButton")
        self.layout_button.setIcon(svg_icon("image"))
        self.layout_button.setToolTip("切换在线阅读的图片缩放布局")
        self.layout_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.layout_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.layout_menu = QMenu(self.layout_button)
        self.layout_menu.setObjectName("readerLayoutMenu")
        self._layout_action_group = QActionGroup(self)
        self._layout_action_group.setExclusive(True)
        self._layout_actions = {}
        for mode, label in self._LAYOUT_LABELS.items():
            action = self.layout_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, selected=mode: (
                    self._select_layout_mode(selected)
                    if checked
                    else None
                )
            )
            self._layout_action_group.addAction(action)
            self._layout_actions[mode] = action
        self.layout_button.setMenu(self.layout_menu)
        action_row.addWidget(self.layout_button)
        self.zoom_button = QToolButton(self)
        self.zoom_button.setObjectName("readerZoomButton")
        self.zoom_button.setIcon(svg_icon("search"))
        self.zoom_button.setToolTip("切换在线阅读的图片缩放比例")
        self.zoom_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.zoom_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.zoom_menu = QMenu(self.zoom_button)
        self.zoom_menu.setObjectName("readerZoomMenu")
        self._zoom_action_group = QActionGroup(self)
        self._zoom_action_group.setExclusive(True)
        self._zoom_actions = {}
        for percent in sorted(READER_ZOOM_LEVELS):
            action = self.zoom_menu.addAction(f"{percent}%")
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, selected=percent: (
                    self._select_zoom_percent(selected)
                    if checked
                    else None
                )
            )
            self._zoom_action_group.addAction(action)
            self._zoom_actions[percent] = action
        self.zoom_button.setMenu(self.zoom_menu)
        action_row.addWidget(self.zoom_button)
        self.retry_button = self._tool_button(
            "readerRetryButton",
            "重试失败页",
            "refresh",
            self._retry_failed,
        )
        action_row.addWidget(self.retry_button)
        self.download_button = self._tool_button(
            "readerDownloadButton",
            "下载当前章节",
            "download",
            self._download_current,
        )
        action_row.addWidget(self.download_button)
        root.addLayout(action_row)

        rule = QFrame(self)
        rule.setObjectName("readerRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        root.addWidget(rule)

        self.view = ReaderGraphicsView(self)
        self._select_layout_mode(self._layout_mode, persist=False)
        self._select_zoom_percent(self._zoom_percent, persist=False)
        root.addWidget(self.view, 1)

        self.view.viewport_changed.connect(self._on_viewport)
        self.view.retry_requested.connect(self._retry_page)
        controller.catalog_ready.connect(self._on_catalog_ready)
        controller.chapter_ready.connect(self._on_chapter_ready)
        controller.page_loading.connect(self._on_page_loading)
        controller.page_ready.connect(self._on_page_ready)
        controller.page_failed.connect(self._on_page_failed)
        controller.operation_failed.connect(self._on_operation_failed)
        controller.history_failed.connect(self._show_error)
        if settings_controller is not None:
            settings_controller.settings_changed.connect(
                self._on_settings_changed
            )
        self._refresh_controls()

    def open_album(
        self,
        album_id: str,
        *,
        title: str | None,
        source: ReaderSource,
        preferred_photo_id: str | None = None,
        preferred_page: int = 1,
    ) -> int:
        if not isinstance(source, ReaderSource):
            raise TypeError("source must be ReaderSource")
        self._source = source
        self._album_title = (title or f"JM {album_id}").strip()
        self.title_label.setText(self._album_title)
        self._pending_photo_id = preferred_photo_id
        self._pending_page = max(1, int(preferred_page))
        self._catalog = None
        self._chapter = None
        self.view.clear_pages()
        self._failed_pages.clear()
        self._show_error("")
        self._generation = self.controller.open_album(album_id)
        self._refresh_controls()
        return self._generation

    def open_catalog(
        self,
        catalog: ChapterCatalogSnapshot,
        *,
        source: ReaderSource,
        photo_id: str | None = None,
        page_number: int = 1,
    ) -> None:
        if not isinstance(catalog, ChapterCatalogSnapshot):
            raise TypeError("catalog must be ChapterCatalogSnapshot")
        if not isinstance(source, ReaderSource):
            raise TypeError("source must be ReaderSource")
        self._source = source
        self._catalog = catalog
        self._chapter = None
        self._album_title = catalog.title or f"JM {catalog.album_id}"
        self.title_label.setText(self._album_title)
        self._pending_photo_id = photo_id
        self._pending_page = max(1, int(page_number))
        self.view.clear_pages()
        self._failed_pages.clear()
        self._show_error("")
        self._refresh_controls()
        self._select_initial_chapter()

    @property
    def current_page(self) -> int:
        return self.view.current_page()

    @property
    def current_photo_id(self) -> str | None:
        return self._chapter.photo_id if self._chapter else None

    @property
    def current_album_id(self) -> str | None:
        return self._catalog.album_id if self._catalog else None

    def show_notice(self, message: str) -> None:
        self._show_error(message)

    def end_session(self, generation: int) -> None:
        self._generation = int(generation)
        self._catalog = None
        self._chapter = None
        self._pending_photo_id = None
        self._pending_page = 1
        self._failed_pages.clear()
        self._album_title = ""
        self.title_label.setText("在线阅读")
        self.view.clear_pages()
        self._show_error("")
        self._update_page_display(0)
        self._refresh_controls()

    def _select_initial_chapter(self) -> None:
        if self._catalog is None:
            return
        photo_id = self._pending_photo_id
        if photo_id is not None and any(
            chapter.photo_id == photo_id
            for chapter in self._catalog.chapters
        ):
            self._load_chapter(photo_id)
            return
        if photo_id is not None:
            self._pending_photo_id = None
            self._pending_page = 1
            QMessageBox.information(
                self,
                "上次阅读章节已不可用",
                "阅读历史中的章节已不在当前远端目录中。"
                "请选择新的起始章节。",
            )
        if len(self._catalog.chapters) == 1:
            self._load_chapter(self._catalog.chapters[0].photo_id)
            return
        if not self._catalog.chapters:
            self._show_error("这部漫画当前没有可阅读章节")
            return
        self._show_chapter_dialog()

    def _load_chapter(self, photo_id: str) -> None:
        if self._catalog is None:
            return
        self._generation = self.controller.load_chapter(
            self._catalog,
            photo_id,
            target_width=max(240, self.view.target_width),
        )
        self._chapter = None
        self.view.clear_pages()
        self._failed_pages.clear()
        self._show_error("")
        self.chapter_selected.emit(photo_id)
        self._refresh_controls()

    @Slot(int, object)
    def _on_catalog_ready(
        self,
        generation: int,
        catalog: ChapterCatalogSnapshot,
    ) -> None:
        if generation != self._generation:
            return
        self._catalog = catalog
        self._album_title = catalog.title or self._album_title
        self.title_label.setText(self._album_title)
        self._select_initial_chapter()

    @Slot(int, object, object)
    def _on_chapter_ready(
        self,
        generation: int,
        chapter: ReaderChapterSnapshot,
        pages,
    ) -> None:
        if generation != self._generation:
            return
        self._chapter = chapter
        self.view.set_pages(tuple(pages))
        target = min(max(1, self._pending_page), chapter.page_count)
        self._pending_page = 1
        self.view.scroll_to_page(target)
        self.controller.set_history_context(
            album_id=self._catalog.album_id,
            title=self._album_title,
            chapter=chapter,
            source=self._source,
        )
        self._update_page_display(target)
        self._refresh_controls()

    @Slot(int, str, int)
    def _on_page_loading(
        self,
        generation: int,
        photo_id: str,
        page_number: int,
    ) -> None:
        if self._matches_page(generation, photo_id):
            self.view.set_page_loading(page_number)

    @Slot(int, object, object)
    def _on_page_ready(
        self,
        generation: int,
        snapshot: ReaderPageSnapshot,
        image,
    ) -> None:
        if not self._matches_page(generation, snapshot.photo_id):
            return
        self._failed_pages.discard(snapshot.page_number)
        self.view.set_page_ready(snapshot, image)
        self._refresh_controls()

    @Slot(int, str, int, str, str)
    def _on_page_failed(
        self,
        generation: int,
        photo_id: str,
        page_number: int,
        _kind: str,
        message: str,
    ) -> None:
        if not self._matches_page(generation, photo_id):
            return
        self._failed_pages.add(page_number)
        self.view.set_page_failed(page_number, message)
        self._refresh_controls()

    @Slot(int, str, str)
    def _on_operation_failed(
        self,
        generation: int,
        _kind: str,
        message: str,
    ) -> None:
        if generation == self._generation:
            self._show_error(message)

    @Slot(int, object, int)
    def _on_viewport(
        self,
        current_page: int,
        visible_pages,
        target_width: int,
    ) -> None:
        if self._chapter is None:
            return
        self._update_page_display(current_page)
        self.view.release_far_pages(current_page)
        self.controller.update_viewport(
            self._chapter.photo_id,
            current_page=current_page,
            visible_pages=visible_pages,
            total_pages=self._chapter.page_count,
            target_width=target_width,
        )

    def _previous_page(self) -> None:
        self.previous_page()

    def previous_page(self) -> None:
        current = self.view.current_page()
        if current > 1:
            self.view.scroll_to_page(current - 1)

    def _next_page(self) -> None:
        self.next_page()

    def next_page(self) -> None:
        current = self.view.current_page()
        if self._chapter and current < self._chapter.page_count:
            self.view.scroll_to_page(current + 1)

    def _previous_chapter(self) -> None:
        chapter = self._adjacent_chapter(-1)
        if chapter is not None:
            self._pending_page = 1
            self._load_chapter(chapter.photo_id)

    def _next_chapter(self) -> None:
        chapter = self._adjacent_chapter(1)
        if chapter is not None:
            self._pending_page = 1
            self._load_chapter(chapter.photo_id)

    def _adjacent_chapter(self, offset: int):
        if self._catalog is None or self._chapter is None:
            return None
        indexes = {
            chapter.photo_id: index
            for index, chapter in enumerate(self._catalog.chapters)
        }
        current = indexes.get(self._chapter.photo_id)
        target = current + offset if current is not None else -1
        if 0 <= target < len(self._catalog.chapters):
            return self._catalog.chapters[target]
        return None

    def _choose_chapter(self) -> None:
        if self._catalog is not None:
            self._show_chapter_dialog()

    def _show_chapter_dialog(self) -> None:
        dialog = ReaderChapterDialog(
            self._catalog,
            current_photo_id=self.current_photo_id,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        photo_id = dialog.selected_photo_id()
        if photo_id and photo_id != self.current_photo_id:
            self._pending_page = 1
            self._load_chapter(photo_id)

    def _preview_slider(self, page_number: int) -> None:
        total = self._chapter.page_count if self._chapter else 0
        self.page_label.setText(f"{page_number} / {total}")

    def _apply_slider(self) -> None:
        if self._chapter is not None:
            target = self.page_slider.sliderPosition()
            blocker = QSignalBlocker(self.page_slider)
            self.page_slider.setValue(target)
            del blocker
            self.view.scroll_to_page(target)

    def _retry_page(self, page_number: int) -> None:
        if self._chapter is None:
            return
        self.controller.retry_pages(
            self._chapter.photo_id,
            (page_number,),
            current_page=max(1, self.view.current_page()),
            total_pages=self._chapter.page_count,
            target_width=self.view.target_width,
        )

    def _retry_failed(self) -> None:
        if self._chapter is None or not self._failed_pages:
            return
        self.controller.retry_pages(
            self._chapter.photo_id,
            tuple(self._failed_pages),
            current_page=max(1, self.view.current_page()),
            total_pages=self._chapter.page_count,
            target_width=self.view.target_width,
        )

    def _download_current(self) -> None:
        if self._chapter is not None:
            self.download_chapter_requested.emit(self._chapter.photo_id)

    def _back(self) -> None:
        self.back_requested.emit()

    def _update_page_display(self, current: int) -> None:
        total = self._chapter.page_count if self._chapter else 0
        blocker = QSignalBlocker(self.page_slider)
        self.page_slider.setRange(1, max(1, total))
        if not self.page_slider.isSliderDown():
            self.page_slider.setValue(max(1, current))
        del blocker
        if not self.page_slider.isSliderDown():
            self.page_label.setText(
                f"{current} / {total}" if total else "0 / 0"
            )
        self._refresh_controls()

    def layout_action(self, mode: str):
        return self._layout_actions[mode]

    def zoom_action(self, percent: int):
        return self._zoom_actions[percent]

    def _select_layout_mode(
        self,
        mode: str,
        *,
        persist: bool = True,
    ) -> None:
        if mode not in READER_LAYOUT_MODES:
            raise ValueError("reader layout mode is invalid")
        self._layout_mode = mode
        if hasattr(self, "view"):
            self.view.set_layout_mode(mode)
        self.layout_button.setText(
            f"阅读视图：{self._LAYOUT_LABELS[mode]}"
        )
        for value, action in self._layout_actions.items():
            action.setChecked(value == mode)
        if not persist or self.settings_controller is None:
            return
        current = self.settings_controller.settings
        if current.reader_layout == mode:
            return
        if not self.settings_controller.save(
            replace(current, reader_layout=mode)
        ):
            fallback = self.settings_controller.settings.reader_layout
            self._select_layout_mode(fallback, persist=False)
            self._show_error("阅读视图设置无法保存")

    def _select_zoom_percent(
        self,
        percent: int,
        *,
        persist: bool = True,
    ) -> None:
        if (
            type(percent) is not int
            or percent not in READER_ZOOM_LEVELS
        ):
            raise ValueError("reader zoom percent is invalid")
        self._zoom_percent = percent
        if hasattr(self, "view"):
            self.view.set_zoom_percent(percent)
        self.zoom_button.setText(f"缩放：{percent}%")
        for value, action in self._zoom_actions.items():
            action.setChecked(value == percent)
        if not persist or self.settings_controller is None:
            return
        current = self.settings_controller.settings
        if current.reader_zoom_percent == percent:
            return
        if not self.settings_controller.save(
            replace(current, reader_zoom_percent=percent)
        ):
            fallback = self.settings_controller.settings.reader_zoom_percent
            self._select_zoom_percent(fallback, persist=False)
            self._show_error("阅读缩放设置无法保存")

    @Slot(object)
    def _on_settings_changed(self, settings) -> None:
        mode = getattr(settings, "reader_layout", "fit_width")
        if mode in READER_LAYOUT_MODES:
            self._select_layout_mode(mode, persist=False)
        percent = getattr(settings, "reader_zoom_percent", 100)
        if percent in READER_ZOOM_LEVELS:
            self._select_zoom_percent(percent, persist=False)

    def _refresh_controls(self) -> None:
        total = self._chapter.page_count if self._chapter else 0
        current = self.view.current_page()
        self.previous_page_button.setEnabled(total > 0 and current > 1)
        self.next_page_button.setEnabled(
            total > 0 and current < total
        )
        self.page_slider.setEnabled(total > 0)
        self.chapter_button.setEnabled(self._catalog is not None)
        self.previous_chapter_button.setEnabled(
            self._adjacent_chapter(-1) is not None
        )
        self.next_chapter_button.setEnabled(
            self._adjacent_chapter(1) is not None
        )
        self.retry_button.setEnabled(bool(self._failed_pages))
        self.download_button.setEnabled(self._chapter is not None)
        if self._chapter is not None:
            self.chapter_button.setText(
                f"第 {self._chapter.index} 章 · {self._chapter.title}"
            )
        else:
            self.chapter_button.setText("选择章节")

    def _show_error(self, message: str) -> None:
        message = str(message).strip()
        self.error_banner.setText(message)
        self.error_banner.setVisible(bool(message))

    def _matches_page(self, generation: int, photo_id: str) -> bool:
        return (
            generation == self._generation
            and self._chapter is not None
            and self._chapter.photo_id == photo_id
        )

    def _tool_button(
        self,
        object_name: str,
        text: str,
        icon: str,
        callback,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setText(text)
        button.setIcon(svg_icon(icon))
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        button.clicked.connect(callback)
        return button


__all__ = ["ReaderPage"]
