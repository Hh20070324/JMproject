from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QDialog, QWidget

from ..models import ChapterCatalogSnapshot
from ..tasks import InvalidAlbumId, normalize_album_id
from .widgets.chapter_selection_dialog import ChapterSelectionDialog


class ChapterDownloadFlow(QObject):
    """Coordinate chapter lookup, selection, and task creation for Qt pages."""

    loading_changed = Signal(str, bool)
    catalog_resolved = Signal(str, object)
    task_created = Signal(str, object)
    task_skipped = Signal(str)
    failed = Signal(str, str)

    def __init__(
        self,
        download_controller,
        chapter_controller=None,
        parent: QWidget | None = None,
        dialog_factory=ChapterSelectionDialog,
    ):
        super().__init__(parent)
        self.download_controller = download_controller
        self.chapter_controller = chapter_controller
        self.dialog_parent = parent
        self.dialog_factory = dialog_factory
        self._pending: dict[int, str] = {}
        self._disposed = False

        if chapter_controller is not None:
            chapter_controller.catalog_ready.connect(self._on_catalog_ready)
            chapter_controller.catalog_failed.connect(self._on_catalog_failed)

    def start(
        self,
        album_id: str,
        catalog: ChapterCatalogSnapshot | None = None,
    ) -> str | None:
        if self._disposed or self.download_controller is None:
            return None
        try:
            normalized_id = str(int(normalize_album_id(album_id)))
        except (InvalidAlbumId, TypeError, ValueError) as error:
            self.failed.emit("", str(error) or "请输入有效的 JM 号")
            return None

        if catalog is not None:
            if catalog.album_id != normalized_id or not catalog.chapters:
                self.failed.emit(normalized_id, "章节目录无效，请重新读取")
                return None
            if self.chapter_controller is not None:
                self.chapter_controller.prime(catalog)
            self.catalog_resolved.emit(normalized_id, catalog)
            self._create_selected_task(normalized_id, catalog)
            return normalized_id

        # Keep controller-less test and recovery surfaces compatible. The
        # production app always supplies the asynchronous chapter controller.
        if self.chapter_controller is None:
            snapshot = self.download_controller.add_task(normalized_id)
            if snapshot is not None:
                self.task_created.emit(normalized_id, snapshot)
            else:
                self.task_skipped.emit(normalized_id)
            return normalized_id

        request_id = self.chapter_controller.request(normalized_id)
        if request_id is None:
            self.failed.emit(normalized_id, "无法读取章节，请检查 JM 号后重试")
            return None
        if request_id not in self._pending:
            self._pending[request_id] = normalized_id
            self.loading_changed.emit(normalized_id, True)
        return normalized_id

    @Slot()
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._pending.clear()

    @Slot(int, object)
    def _on_catalog_ready(
        self,
        request_id: int,
        catalog: ChapterCatalogSnapshot,
    ) -> None:
        if self._disposed:
            return
        album_id = self._pending.pop(request_id, None)
        if album_id is None:
            return
        self.loading_changed.emit(album_id, False)
        if (
            not isinstance(catalog, ChapterCatalogSnapshot)
            or catalog.album_id != album_id
            or not catalog.chapters
        ):
            self.failed.emit(album_id, "章节目录无效，请重新读取")
            return
        self.catalog_resolved.emit(album_id, catalog)
        self._create_selected_task(album_id, catalog)

    @Slot(int, str, str)
    def _on_catalog_failed(
        self,
        request_id: int,
        _code: str,
        message: str,
    ) -> None:
        if self._disposed:
            return
        album_id = self._pending.pop(request_id, None)
        if album_id is None:
            return
        self.loading_changed.emit(album_id, False)
        self.failed.emit(album_id, message)

    def _create_selected_task(
        self,
        album_id: str,
        catalog: ChapterCatalogSnapshot,
    ) -> None:
        chapters = catalog.chapters
        if len(chapters) == 1:
            selected_ids = (chapters[0].photo_id,)
        else:
            dialog = self.dialog_factory(catalog, self.dialog_parent)
            try:
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    self.task_skipped.emit(album_id)
                    return
                selected_ids = dialog.selected_chapter_ids()
            finally:
                dialog.deleteLater()
            if not selected_ids:
                self.task_skipped.emit(album_id)
                return

        snapshot = self.download_controller.add_task(
            album_id,
            selected_chapter_ids=selected_ids,
        )
        if snapshot is not None:
            self.task_created.emit(album_id, snapshot)
        else:
            self.task_skipped.emit(album_id)


__all__ = ["ChapterDownloadFlow"]
