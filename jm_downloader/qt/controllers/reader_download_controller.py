import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot

from ...models import (
    ReaderChapterDownloadSnapshot,
    ReaderChapterDownloadState,
    TaskSnapshot,
    TaskStatus,
)

LOGGER = logging.getLogger("jm-downloader")


_TASK_MESSAGES = {
    TaskStatus.PENDING: "当前章节已有等待任务",
    TaskStatus.FETCHING: "当前章节正在读取任务信息",
    TaskStatus.DOWNLOADING: "当前章节下载中",
    TaskStatus.PAUSING: "当前章节暂停中",
    TaskStatus.PAUSED: "当前章节已暂停",
    TaskStatus.CANCELLING: "当前章节取消中",
    TaskStatus.FAILED: "当前章节已有失败任务",
}


class ReaderDownloadController(QObject):
    state_changed = Signal(object)
    _detection_finished = Signal(int, str, str, object, str)

    def __init__(
        self,
        downloaded_detector,
        download_controller,
        parent=None,
    ):
        super().__init__(parent)
        if not callable(downloaded_detector):
            raise TypeError("downloaded_detector must be callable")
        self._downloaded_detector = downloaded_detector
        self._download_controller = download_controller
        self._generation = 0
        self._current: tuple[str, str] | None = None
        self._completed_ids: frozenset[str] | None = None
        self._tasks = tuple(download_controller.list_tasks())
        self._condition = threading.Condition()
        self._pending_request = None
        self._stopping = False
        self._worker = threading.Thread(
            target=self._run,
            name="reader-download-state",
            daemon=True,
        )
        self._detection_finished.connect(self._on_detection_finished)
        download_controller.tasks_reset.connect(self._on_tasks_reset)
        self._worker.start()

    @property
    def current(self) -> tuple[str, str] | None:
        return self._current

    def request(self, album_id: str, photo_id: str) -> None:
        album_id = self._validated_id(album_id, "album_id")
        photo_id = self._validated_id(photo_id, "photo_id")
        self._generation += 1
        generation = self._generation
        self._current = (album_id, photo_id)
        self._completed_ids = None
        self.state_changed.emit(
            ReaderChapterDownloadSnapshot(
                album_id,
                photo_id,
                ReaderChapterDownloadState.CHECKING,
                "正在检查下载状态…",
            )
        )
        with self._condition:
            if self._stopping:
                return
            self._pending_request = (generation, album_id, photo_id)
            self._condition.notify()

    def retry(self) -> None:
        if self._current is not None:
            self.request(*self._current)

    def clear(self) -> None:
        self._generation += 1
        self._current = None
        self._completed_ids = None
        with self._condition:
            self._pending_request = None

    def refresh_tasks(self) -> None:
        self._on_tasks_reset(self._download_controller.list_tasks())

    def dispose(self) -> None:
        self.clear()
        with self._condition:
            self._stopping = True
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while (
                    self._pending_request is None
                    and not self._stopping
                ):
                    self._condition.wait()
                if self._stopping:
                    return
                generation, album_id, photo_id = self._pending_request
                self._pending_request = None
            try:
                completed = frozenset(
                    str(value)
                    for value in self._downloaded_detector(album_id)
                )
                error = ""
            except Exception as failure:
                LOGGER.warning(
                    "Reader download-state scan failed (%s)",
                    type(failure).__name__,
                )
                completed = frozenset()
                error = "无法确认下载状态"
            with self._condition:
                if self._stopping:
                    return
            self._detection_finished.emit(
                generation,
                album_id,
                photo_id,
                completed,
                error,
            )

    @Slot(int, str, str, object, str)
    def _on_detection_finished(
        self,
        generation: int,
        album_id: str,
        photo_id: str,
        completed_ids,
        error: str,
    ) -> None:
        if (
            generation != self._generation
            or self._current != (album_id, photo_id)
        ):
            return
        if error:
            self._completed_ids = None
            self.state_changed.emit(
                ReaderChapterDownloadSnapshot(
                    album_id,
                    photo_id,
                    ReaderChapterDownloadState.UNKNOWN,
                    "无法确认下载状态",
                )
            )
            return
        self._completed_ids = frozenset(completed_ids)
        self._publish_combined()

    @Slot(object)
    def _on_tasks_reset(self, snapshots) -> None:
        previous = self._tasks
        self._tasks = tuple(
            snapshot
            for snapshot in snapshots
            if isinstance(snapshot, TaskSnapshot)
        )
        if self._current is None:
            return
        album_id, photo_id = self._current
        if self._completed_transition(
            previous,
            self._tasks,
            album_id,
            photo_id,
        ):
            self.request(album_id, photo_id)
            return
        if self._completed_ids is not None:
            self._publish_combined()

    def _publish_combined(self) -> None:
        if self._current is None or self._completed_ids is None:
            return
        album_id, photo_id = self._current
        reserved = self._reserved_task(album_id, photo_id, self._tasks)
        if reserved is not None:
            self.state_changed.emit(
                ReaderChapterDownloadSnapshot(
                    album_id,
                    photo_id,
                    ReaderChapterDownloadState.TASK_RESERVED,
                    _TASK_MESSAGES.get(
                        reserved.status,
                        "当前章节已有任务",
                    ),
                    task_status=reserved.status,
                )
            )
            return
        if photo_id in self._completed_ids:
            state = ReaderChapterDownloadState.DOWNLOADED
            message = "当前章节已下载"
        else:
            state = ReaderChapterDownloadState.AVAILABLE
            message = "下载当前章节"
        self.state_changed.emit(
            ReaderChapterDownloadSnapshot(
                album_id,
                photo_id,
                state,
                message,
            )
        )

    @staticmethod
    def _reserved_task(
        album_id: str,
        photo_id: str,
        snapshots,
    ) -> TaskSnapshot | None:
        for snapshot in snapshots:
            if (
                snapshot.album_id != album_id
                or snapshot.status is TaskStatus.COMPLETED
            ):
                continue
            selected = snapshot.selected_chapter_ids
            if selected is None or photo_id in selected:
                return snapshot
        return None

    @classmethod
    def _completed_transition(
        cls,
        previous,
        current,
        album_id: str,
        photo_id: str,
    ) -> bool:
        previous_by_id = {snapshot.id: snapshot for snapshot in previous}
        for snapshot in current:
            if (
                snapshot.status is not TaskStatus.COMPLETED
                or snapshot.album_id != album_id
            ):
                continue
            selected = snapshot.selected_chapter_ids
            if selected is not None and photo_id not in selected:
                continue
            old = previous_by_id.get(snapshot.id)
            if old is not None and old.status is not TaskStatus.COMPLETED:
                return True
        return False

    @staticmethod
    def _validated_id(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or not value.isascii()
            or not value.isdigit()
        ):
            raise ValueError(f"{label} is invalid")
        return str(int(value))


__all__ = ["ReaderDownloadController"]
