import logging
import threading
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...models import SearchMode, SearchPageSnapshot, SearchRequest
from ...search import (
    SearchError,
    SearchNotFound,
    SearchRejected,
    SearchResponseError,
    SearchService,
    SearchUnavailable,
    SearchValidationError,
    normalize_search_request,
)


LOGGER = logging.getLogger("jm-downloader")
DEFAULT_WORKER_COUNT = 2
DEFAULT_RESULT_INTERVAL_MS = 15
MAX_WORKER_COUNT = 4


@dataclass(frozen=True, slots=True)
class _SearchJob:
    generation: int
    request: SearchRequest
    is_page_change: bool


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    job: _SearchJob
    snapshot: SearchPageSnapshot | None = None
    error_code: str | None = None
    error_message: str | None = None


class _MetadataMailbox:
    """Thread-safe latest-only work slot without any QObject references."""

    def __init__(self, service: SearchService):
        self.service = service
        self.condition = threading.Condition()
        self.pending: _SearchJob | None = None
        self.completed: deque[_SearchOutcome] = deque()
        self.latest_generation = 0
        self.stopped = False

    def submit(self, job: _SearchJob) -> bool:
        with self.condition:
            if self.stopped:
                return False
            self.latest_generation = job.generation
            self.pending = job
            self.completed.clear()
            self.condition.notify()
            return True

    def next_job(self) -> _SearchJob | None:
        with self.condition:
            while self.pending is None and not self.stopped:
                self.condition.wait()
            if self.stopped:
                return None
            job = self.pending
            self.pending = None
            return job

    def publish(self, outcome: _SearchOutcome) -> None:
        with self.condition:
            if self.stopped:
                return
            if outcome.job.generation != self.latest_generation:
                return
            self.completed.append(outcome)

    def take_completed(self) -> tuple[_SearchOutcome, ...]:
        with self.condition:
            outcomes = tuple(self.completed)
            self.completed.clear()
            return outcomes

    def pending_count(self) -> int:
        with self.condition:
            return int(self.pending is not None)

    def close(self, *_args) -> None:
        with self.condition:
            if self.stopped:
                return
            self.stopped = True
            self.latest_generation += 1
            self.pending = None
            self.completed.clear()
            self.condition.notify_all()


def _metadata_worker(mailbox: _MetadataMailbox) -> None:
    while True:
        job = mailbox.next_job()
        if job is None:
            return

        try:
            snapshot = mailbox.service.search(job.request)
            if not isinstance(snapshot, SearchPageSnapshot):
                raise SearchResponseError()
            if snapshot.request != job.request:
                raise SearchResponseError()
            outcome = _SearchOutcome(job=job, snapshot=snapshot)
        except Exception as error:
            error_code, error_message = _safe_error_payload(error)
            LOGGER.warning(
                "Search worker failed: generation=%s mode=%s page=%s "
                "category=%s error_type=%s",
                job.generation,
                job.request.mode.value,
                job.request.page,
                error_code,
                type(error).__name__,
            )
            outcome = _SearchOutcome(
                job=job,
                error_code=error_code,
                error_message=error_message,
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


class SearchController(QObject):
    search_submitted = Signal(int, object)
    results_ready = Signal(int, object, bool)
    empty_results = Signal(int, object, bool)
    search_failed = Signal(int, str, str, bool)
    validation_failed = Signal(str, str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        service: SearchService | None = None,
        parent=None,
        worker_count: int = DEFAULT_WORKER_COUNT,
        result_interval_ms: int = DEFAULT_RESULT_INTERVAL_MS,
    ):
        super().__init__(parent)
        if type(worker_count) is not int or not 1 <= worker_count <= MAX_WORKER_COUNT:
            raise ValueError("worker_count must be between 1 and 4")
        if type(result_interval_ms) is not int or result_interval_ms < 1:
            raise ValueError("result_interval_ms must be a positive integer")

        self.service = service if service is not None else SearchService()
        self._mailbox = _MetadataMailbox(self.service)
        self._workers: tuple[threading.Thread, ...] = ()
        self._generation = 0
        self._current_request: SearchRequest | None = None
        self._current_snapshot: SearchPageSnapshot | None = None
        self._last_job: _SearchJob | None = None
        self._busy = False
        self._disposed = False

        workers = []
        try:
            for index in range(worker_count):
                worker = threading.Thread(
                    target=_metadata_worker,
                    args=(self._mailbox,),
                    name=f"jm-search-{index + 1}",
                    daemon=True,
                )
                worker.start()
                workers.append(worker)
        except Exception:
            self._mailbox.close()
            raise
        self._workers = tuple(workers)

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(result_interval_ms)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self.destroyed.connect(self._mailbox.close)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def current_request(self) -> SearchRequest | None:
        return self._current_request

    @property
    def current_snapshot(self) -> SearchPageSnapshot | None:
        return self._current_snapshot

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def workers_are_daemon(self) -> bool:
        return all(worker.daemon for worker in self._workers)

    @property
    def pending_request_count(self) -> int:
        return self._mailbox.pending_count()

    @Slot(object)
    def submit(
        self,
        request: SearchRequest,
        *,
        is_page_change: bool = False,
    ) -> int | None:
        if self._disposed:
            return None
        try:
            request = normalize_search_request(request)
        except SearchValidationError as error:
            self.validation_failed.emit(error.code, str(error))
            return None

        self._generation += 1
        job = _SearchJob(self._generation, request, bool(is_page_change))
        if not self._mailbox.submit(job):
            return None
        self._last_job = job
        self.search_submitted.emit(job.generation, job.request)
        self._set_busy(True)
        return job.generation

    def search(
        self,
        mode: SearchMode,
        query: str,
        page: int = 1,
    ) -> int | None:
        return self.submit(SearchRequest(mode, query, page))

    @Slot(int)
    def change_page(self, page: int) -> int | None:
        if self._disposed:
            return None
        snapshot = self._current_snapshot
        if snapshot is None:
            self.validation_failed.emit(
                SearchValidationError.code,
                "当前没有可翻页的搜索结果",
            )
            return None
        if type(page) is not int or page < 1:
            self.validation_failed.emit(
                SearchValidationError.code,
                "页码必须是正整数",
            )
            return None
        page_count = snapshot.page_count
        if page_count == 0 or page > page_count:
            self.validation_failed.emit(
                SearchValidationError.code,
                "页码超出搜索结果范围",
            )
            return None
        if page == snapshot.request.page:
            return None
        request = SearchRequest(
            snapshot.request.mode,
            snapshot.request.query,
            page,
        )
        return self.submit(request, is_page_change=True)

    @Slot()
    def retry(self) -> int | None:
        if self._disposed or self._last_job is None:
            return None
        return self.submit(
            self._last_job.request,
            is_page_change=self._last_job.is_page_change,
        )

    @Slot()
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._result_timer.stop()
        self._mailbox.close()
        self._busy = False

    @Slot()
    def _drain_results(self) -> None:
        if self._disposed:
            return
        for outcome in self._mailbox.take_completed():
            if self._disposed or outcome.job.generation != self._generation:
                continue
            self._set_busy(False)
            if self._disposed or outcome.job.generation != self._generation:
                continue

            if outcome.snapshot is None:
                self.search_failed.emit(
                    outcome.job.generation,
                    outcome.error_code or SearchError.code,
                    outcome.error_message or SearchError.default_message,
                    outcome.job.is_page_change,
                )
                continue

            self._current_request = outcome.snapshot.request
            self._current_snapshot = outcome.snapshot
            signal = (
                self.results_ready
                if outcome.snapshot.items
                else self.empty_results
            )
            signal.emit(
                outcome.job.generation,
                outcome.snapshot,
                outcome.job.is_page_change,
            )

    def _set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        self.busy_changed.emit(busy)


__all__ = [
    "DEFAULT_RESULT_INTERVAL_MS",
    "DEFAULT_WORKER_COUNT",
    "SearchController",
]
