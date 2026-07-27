from bisect import bisect_right
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ...models import ReaderPageSnapshot, ReaderPageState


PAGE_GAP = 16
PAGE_MARGIN = 16
DEFAULT_PAGE_RATIO = 1.42
MIN_PAGE_WIDTH = 240


@dataclass(slots=True)
class _PageVisual:
    snapshot: ReaderPageSnapshot
    background: QGraphicsRectItem
    pixmap: QGraphicsPixmapItem
    message: QGraphicsSimpleTextItem
    display_width: float = 0
    display_height: float = 0
    image_bytes: int = 0


class ReaderGraphicsView(QGraphicsView):
    viewport_changed = Signal(int, object, int)
    retry_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reader_scene = QGraphicsScene(self)
        self.setScene(self._reader_scene)
        self.setObjectName("readerGraphicsView")
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate
        )
        self._pages: list[_PageVisual] = []
        self._tops: list[float] = []
        self._target_width = MIN_PAGE_WIDTH
        self._viewport_timer = QTimer(self)
        self._viewport_timer.setSingleShot(True)
        self._viewport_timer.setInterval(25)
        self._viewport_timer.timeout.connect(self._emit_viewport)
        self.verticalScrollBar().valueChanged.connect(
            self._schedule_viewport
        )

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def target_width(self) -> int:
        return self._target_width

    @property
    def loaded_image_bytes(self) -> int:
        return sum(page.image_bytes for page in self._pages)

    def set_pages(
        self,
        pages: tuple[ReaderPageSnapshot, ...],
    ) -> None:
        self.scene().clear()
        self._pages.clear()
        self._tops.clear()
        for snapshot in pages:
            if not isinstance(snapshot, ReaderPageSnapshot):
                raise TypeError("pages must contain ReaderPageSnapshot")
            background = self.scene().addRect(QRectF())
            pixmap = self.scene().addPixmap(QPixmap())
            message = self.scene().addSimpleText(
                self._state_message(snapshot.state)
            )
            pixmap.setZValue(1)
            message.setZValue(2)
            self._pages.append(
                _PageVisual(
                    snapshot,
                    background,
                    pixmap,
                    message,
                )
            )
        self._apply_palette()
        self._relayout(preserve_anchor=False)
        self.verticalScrollBar().setValue(0)
        self._schedule_viewport()

    def clear_pages(self) -> None:
        self.scene().clear()
        self._pages.clear()
        self._tops.clear()
        self._schedule_viewport()

    def set_page_loading(self, page_number: int) -> None:
        page = self._page(page_number)
        page.snapshot = ReaderPageSnapshot(
            page.snapshot.photo_id,
            page_number,
            page.snapshot.total_pages,
            ReaderPageState.LOADING,
            width=page.snapshot.width,
            height=page.snapshot.height,
            cache_path=page.snapshot.cache_path,
        )
        page.message.setText("正在加载…")
        page.message.show()

    def set_page_failed(self, page_number: int, message: str) -> None:
        page = self._page(page_number)
        page.pixmap.setPixmap(QPixmap())
        page.image_bytes = 0
        page.snapshot = ReaderPageSnapshot(
            page.snapshot.photo_id,
            page_number,
            page.snapshot.total_pages,
            ReaderPageState.FAILED,
            width=page.snapshot.width,
            height=page.snapshot.height,
            cache_path=None,
        )
        page.message.setText(
            f"第 {page_number} 页加载失败\n{message}\n双击此页重试"
        )
        page.message.show()

    def set_page_ready(
        self,
        snapshot: ReaderPageSnapshot,
        image: QImage,
    ) -> None:
        if (
            not isinstance(snapshot, ReaderPageSnapshot)
            or snapshot.state is not ReaderPageState.READY
            or not isinstance(image, QImage)
            or image.isNull()
        ):
            return
        page = self._page(snapshot.page_number)
        anchor = self._anchor()
        page.snapshot = snapshot
        page.pixmap.setPixmap(QPixmap.fromImage(image))
        page.image_bytes = max(0, image.sizeInBytes())
        page.message.hide()
        self._relayout(anchor=anchor)
        self._schedule_viewport()

    def release_far_pages(
        self,
        current_page: int,
        *,
        keep_before: int = 2,
        keep_after: int = 3,
    ) -> tuple[int, ...]:
        released = []
        lower = max(1, current_page - keep_before)
        upper = min(self.page_count, current_page + keep_after)
        for page_number, page in enumerate(self._pages, start=1):
            if (
                lower <= page_number <= upper
                or page.pixmap.pixmap().isNull()
            ):
                continue
            page.pixmap.setPixmap(QPixmap())
            page.image_bytes = 0
            page.message.setText("滚动到此页时加载")
            page.message.show()
            released.append(page_number)
        return tuple(released)

    def current_page(self) -> int:
        if not self._pages:
            return 0
        center_y = self.mapToScene(
            self.viewport().rect().center()
        ).y()
        index = max(0, min(len(self._tops) - 1, bisect_right(
            self._tops,
            center_y,
        ) - 1))
        candidates = {index}
        if index + 1 < len(self._pages):
            candidates.add(index + 1)
        if index > 0:
            candidates.add(index - 1)
        nearest = min(
            candidates,
            key=lambda value: abs(
                self._tops[value]
                + self._pages[value].display_height / 2
                - center_y
            ),
        )
        return nearest + 1

    def visible_pages(self) -> tuple[int, ...]:
        if not self._pages:
            return ()
        viewport_rect = self.mapToScene(
            self.viewport().rect()
        ).boundingRect()
        start = max(
            0,
            bisect_right(self._tops, viewport_rect.top()) - 1,
        )
        values = []
        for index in range(start, len(self._pages)):
            top = self._tops[index]
            page = self._pages[index]
            if top > viewport_rect.bottom():
                break
            if top + page.display_height >= viewport_rect.top():
                values.append(index + 1)
        return tuple(values) or (self.current_page(),)

    def scroll_to_page(self, page_number: int) -> None:
        page = self._page(page_number)
        self.verticalScrollBar().setValue(
            int(round(page.background.pos().y()))
        )
        self._schedule_viewport()

    def page_top(self, page_number: int) -> float:
        self._page(page_number)
        return self._tops[page_number - 1]

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        width = max(
            MIN_PAGE_WIDTH,
            self.viewport().width() - PAGE_MARGIN * 2,
        )
        if width != self._target_width:
            self._target_width = width
            self._relayout()
        self._schedule_viewport()

    def mouseDoubleClickEvent(self, event) -> None:
        page_number = self._page_at_scene_y(
            self.mapToScene(event.position().toPoint()).y()
        )
        if (
            page_number > 0
            and self._pages[page_number - 1].snapshot.state
            is ReaderPageState.FAILED
        ):
            self.retry_requested.emit(page_number)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        ):
            self._apply_palette()

    def _relayout(
        self,
        *,
        anchor=None,
        preserve_anchor: bool = True,
    ) -> None:
        if not self._pages:
            self.scene().setSceneRect(QRectF())
            return
        anchor = (
            anchor
            if anchor is not None
            else self._anchor() if preserve_anchor else None
        )
        y = 0.0
        self._tops = []
        for page in self._pages:
            self._tops.append(y)
            width = float(self._target_width)
            if (
                page.snapshot.width
                and page.snapshot.height
                and page.snapshot.width > 0
                and page.snapshot.height > 0
            ):
                height = (
                    width
                    * page.snapshot.height
                    / page.snapshot.width
                )
            else:
                height = width * DEFAULT_PAGE_RATIO
            page.display_width = width
            page.display_height = max(120.0, height)
            page.background.setRect(
                0,
                0,
                page.display_width,
                page.display_height,
            )
            page.background.setPos(PAGE_MARGIN, y)
            page.pixmap.setPos(PAGE_MARGIN, y)
            pixmap = page.pixmap.pixmap()
            if not pixmap.isNull():
                page.pixmap.setScale(
                    page.display_width / max(1, pixmap.width())
                )
            text_rect = page.message.boundingRect()
            page.message.setPos(
                PAGE_MARGIN + max(
                    12,
                    (page.display_width - text_rect.width()) / 2,
                ),
                y + max(
                    12,
                    (page.display_height - text_rect.height()) / 2,
                ),
            )
            y += page.display_height + PAGE_GAP
        scene_width = self._target_width + PAGE_MARGIN * 2
        self.scene().setSceneRect(
            0,
            0,
            scene_width,
            max(0.0, y - PAGE_GAP),
        )
        if anchor is not None:
            page_number, offset = anchor
            if 1 <= page_number <= len(self._pages):
                target_center = self._tops[page_number - 1] + offset
                current_center = self.mapToScene(
                    self.viewport().rect().center()
                ).y()
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value()
                    + int(round(target_center - current_center))
                )

    def _anchor(self):
        page_number = self.current_page()
        if page_number < 1:
            return None
        center_y = self.mapToScene(
            self.viewport().rect().center()
        ).y()
        return page_number, center_y - self._tops[page_number - 1]

    def _page(self, page_number: int) -> _PageVisual:
        if (
            type(page_number) is not int
            or not 1 <= page_number <= len(self._pages)
        ):
            raise ValueError("page number is out of range")
        return self._pages[page_number - 1]

    def _page_at_scene_y(self, scene_y: float) -> int:
        if not self._pages:
            return 0
        index = max(0, bisect_right(self._tops, scene_y) - 1)
        if index >= len(self._pages):
            return 0
        page = self._pages[index]
        if scene_y <= self._tops[index] + page.display_height:
            return index + 1
        return 0

    def _schedule_viewport(self, *_args) -> None:
        self._viewport_timer.start()

    def _emit_viewport(self) -> None:
        current = self.current_page()
        if current < 1:
            return
        self.viewport_changed.emit(
            current,
            self.visible_pages(),
            self._target_width,
        )

    def _apply_palette(self) -> None:
        palette = self.palette()
        background = palette.color(palette.ColorRole.Base)
        border = palette.color(palette.ColorRole.Mid)
        text = palette.color(palette.ColorRole.Text)
        for page in self._pages:
            page.background.setBrush(QBrush(background))
            page.background.setPen(QPen(border, 1))
            page.message.setBrush(QBrush(text))

    @staticmethod
    def _state_message(state: ReaderPageState) -> str:
        return {
            ReaderPageState.PLACEHOLDER: "滚动到此页时加载",
            ReaderPageState.LOADING: "正在加载…",
            ReaderPageState.READY: "",
            ReaderPageState.FAILED: "加载失败，双击重试",
        }[state]


__all__ = ["ReaderGraphicsView"]
