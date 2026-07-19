import logging
import threading
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...models import ChapterCatalogSnapshot
from ...search import (
    SearchError,
    SearchNotFound,
    SearchRejected,
    SearchResponseError,
    SearchService,
    SearchUnavailable,
    SearchValidationError,
)
from ...tasks import InvalidAlbumId, normalize_album_id


LOGGER = logging.getLogger("jm-downloader")
DEFAULT_RESULT_INTERVAL_MS = 15


@dataclass(frozen=True, slots=True)
class _ChapterJob:
    request_id: int
    album_id: str


@dataclass(frozen=True, slots=True)
class _ChapterOutcome:
    job: _ChapterJob
    catalog: ChapterCatalogSnapshot | None = None
    error_code: str | None = None
    error_message: str | None = None


class _ChapterMailbox:
    """Thread-safe queues that do not retain QObject or widget references."""

    def __init__(self, service):
        self.service = service
        self.condition = threading.Condition()
        self.pending: deque[_ChapterJob] = deque()
        self.completed: deque[_ChapterOutcome] = deque()
        self.stopped = False

    def submit(self, job: _ChapterJob) -> bool:
        with self.condition:
            if self.stopped:
                return False
            self.pending.append(job)
            self.condition.notify()
            return True

    def next_job(self) -> _ChapterJob | None:
        with self.condition:
            while not self.pending and not self.stopped:
                self.condition.wait()
            if self.stopped:
                return None
            return self.pending.popleft()

    def publish(self, outcome: _ChapterOutcome) -> None:
        with self.condition:
            if not self.stopped:
                self.completed.append(outcome)

    def take_completed(self) -> tuple[_ChapterOutcome, ...]:
        with self.condition:
            outcomes = tuple(self.completed)
            self.completed.clear()
            return outcomes

    def close(self, *_args) -> None:
        with self.condition:
            if self.stopped:
                return
            self.stopped = True
            self.pending.clear()
            self.completed.clear()
            self.condition.notify_all()


def _chapter_worker(mailbox: _ChapterMailbox) -> None:
    while True:
        job = mailbox.next_job()
        if job is None:
            return
        try:
            catalog = mailbox.service.fetch_chapters(job.album_id)
            if (
                not isinstance(catalog, ChapterCatalogSnapshot)
                or catalog.album_id != job.album_id
            ):
                raise SearchResponseError()
            outcome = _ChapterOutcome(job, catalog=catalog)
        except Exception as error:
            code, message = _safe_error_payload(error)
            LOGGER.warning(
                "Chapter catalog worker failed: album_id=%s category=%s "
                "error_type=%s",
                job.album_id,
                code,
                type(error).__name__,
            )
            outcome = _ChapterOutcome(
                job,
                error_code=code,
                error_message=message,
            )
        mailbox.publish(outcome)


def _safe_error_payload(error: Exception) -> tuple[str, str]:
    for error_type in (
        SearchValidationError,
        SearchRejected,
        SearchNotFound,
        SearchUnavailable,
        SearchResponseError,
    ):
        if isinstance(error, error_type):
            return error_type.code, error_type.default_message
    return SearchError.code, SearchError.default_message


def _normalize_album_id(value: str) -> str:
    try:
        return str(int(normalize_album_id(value)))
    except (InvalidAlbumId, TypeError, ValueError):
        raise SearchValidationError() from None


class ChapterCatalogController(QObject):
    catalog_ready = Signal(int, object)
    catalog_failed = Signal(int, str, str)
    busy_changed = Signal(str, bool)

    def __init__(
        self,
        service: SearchService | None = None,
        parent=None,
        result_interval_ms: int = DEFAULT_RESULT_INTERVAL_MS,
    ):
        super().__init__(parent)
        if type(result_interval_ms) is not int or result_interval_ms < 1:
            raise ValueError("result_interval_ms must be a positive integer")

        self.service = service if service is not None else SearchService()
        self._mailbox = _ChapterMailbox(self.service)
        self._next_request_id = 0
        self._cache: dict[str, ChapterCatalogSnapshot] = {}
        self._inflight: dict[str, int] = {}
        self._disposed = False

        self._worker = threading.Thread(
            target=_chapter_worker,
            args=(self._mailbox,),
            name="jm-chapter-catalog",
            daemon=True,
        )
        self._worker.start()

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(result_interval_ms)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self.destroyed.connect(self._mailbox.close)

    @Slot(str, result=object)
    def request(self, album_id: str) -> int | None:
        if self._disposed:
            return None
        try:
            album_id = _normalize_album_id(album_id)
        except SearchValidationError:
            return None

        inflight_id = self._inflight.get(album_id)
        if inflight_id is not None:
            return inflight_id

        self._next_request_id += 1
        request_id = self._next_request_id
        job = _ChapterJob(request_id, album_id)
        cached = self._cache.get(album_id)
        if cached is not None:
            self._mailbox.publish(_ChapterOutcome(job, catalog=cached))
            return request_id

        self._inflight[album_id] = request_id
        if not self._mailbox.submit(job):
            self._inflight.pop(album_id, None)
            return None
        self.busy_changed.emit(album_id, True)
        return request_id

    @Slot(object)
    def prime(self, catalog: ChapterCatalogSnapshot) -> None:
        if self._disposed or not isinstance(catalog, ChapterCatalogSnapshot):
            return
        try:
            album_id = _normalize_album_id(catalog.album_id)
        except SearchValidationError:
            return
        if album_id != catalog.album_id:
            return
        self._cache[album_id] = catalog

    def is_busy(self, album_id: str) -> bool:
        if self._disposed:
            return False
        try:
            album_id = _normalize_album_id(album_id)
        except SearchValidationError:
            return False
        return album_id in self._inflight

    @Slot()
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._result_timer.stop()
        self._mailbox.close()
        self._inflight.clear()
        self._cache.clear()

    @Slot()
    def _drain_results(self) -> None:
        if self._disposed:
            return
        for outcome in self._mailbox.take_completed():
            if self._disposed:
                return
            active_request = self._inflight.get(outcome.job.album_id)
            if active_request is not None:
                if active_request != outcome.job.request_id:
                    continue
                self._inflight.pop(outcome.job.album_id, None)
                self.busy_changed.emit(outcome.job.album_id, False)

            if outcome.catalog is not None:
                self._cache[outcome.job.album_id] = outcome.catalog
                self.catalog_ready.emit(
                    outcome.job.request_id,
                    outcome.catalog,
                )
            else:
                self.catalog_failed.emit(
                    outcome.job.request_id,
                    outcome.error_code or SearchError.code,
                    outcome.error_message or SearchError.default_message,
                )


__all__ = ["ChapterCatalogController", "DEFAULT_RESULT_INTERVAL_MS"]
