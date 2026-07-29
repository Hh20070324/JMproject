import asyncio
from collections import deque
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QImage, QImageReader

from ...models import (
    ChapterCatalogSnapshot,
    ReaderChapterSnapshot,
    ReaderContentMode,
    ReaderErrorKind,
    ReaderHistoryEntry,
    ReaderPageSnapshot,
    ReaderSource,
)
from ...reader import (
    MAX_READER_CHAPTER_PAGES,
    ReaderCacheExhausted,
    ReaderHistoryStore,
    ReaderMemoryCache,
    ReaderService,
    ReaderServiceError,
)


LOGGER = logging.getLogger("jm-downloader")
DEFAULT_RESULT_INTERVAL_MS = 15
DEFAULT_HISTORY_DEBOUNCE_MS = 750
MAX_READER_PAGE_QUEUE = 64
MAX_READER_NETWORK_CONCURRENCY = 3
MAX_READER_TARGET_WIDTH = 4_096
MAX_READER_OUTCOMES = 256


@dataclass(frozen=True, slots=True)
class _OpenCommand:
    generation: int
    album_id: str
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE


@dataclass(frozen=True, slots=True)
class _ChapterCommand:
    generation: int
    catalog: ChapterCatalogSnapshot
    photo_id: str
    target_width: int
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE


@dataclass(frozen=True, slots=True)
class _WindowCommand:
    generation: int
    photo_id: str
    current_page: int
    visible_pages: tuple[int, ...]
    total_pages: int
    target_width: int
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE


@dataclass(frozen=True, slots=True)
class _RetryCommand:
    generation: int
    photo_id: str
    page_numbers: tuple[int, ...]
    current_page: int
    total_pages: int
    target_width: int
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE


@dataclass(frozen=True, slots=True)
class _LeaveCommand:
    generation: int


@dataclass(frozen=True, slots=True)
class _HistoryCommand:
    generation: int
    entry: ReaderHistoryEntry


@dataclass(frozen=True, slots=True)
class _StopCommand:
    timeout: float


@dataclass(frozen=True, slots=True)
class _PageJob:
    generation: int
    photo_id: str
    page_number: int
    total_pages: int
    current_page: int
    target_width: int
    priority: int
    sequence: int
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE

    @property
    def key(self) -> tuple[int, ReaderContentMode, str, int, int]:
        return (
            self.generation,
            self.content_mode,
            self.photo_id,
            self.page_number,
            self.target_width,
        )

    @property
    def cache_key(self) -> str:
        return (
            f"{self.generation}:{self.content_mode.value}:{self.photo_id}:"
            f"{self.page_number}:{self.target_width}"
        )

    @property
    def disk_page_key(self) -> str:
        return (
            f"{self.content_mode.value}:"
            f"{self.photo_id}:{self.page_number}"
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    generation: int
    kind: str
    payload: tuple = ()
    error_kind: ReaderErrorKind | None = None
    error_message: str | None = None


class _ReaderBridge:
    def __init__(self):
        self._lock = threading.RLock()
        self._loop = None
        self._runtime = None
        self._pending_commands = deque()
        self._outcomes = deque()
        self.finished = threading.Event()
        self.pending_page_count = 0
        self.inflight_page_count = 0
        self.maximum_network_active = 0
        self.memory_cache_bytes = 0

    def bind(self, loop, runtime) -> None:
        with self._lock:
            self._loop = loop
            self._runtime = runtime
            commands = tuple(self._pending_commands)
            self._pending_commands.clear()
        for command in commands:
            runtime.handle(command)

    def submit(self, command) -> bool:
        with self._lock:
            if self.finished.is_set():
                return False
            if self._loop is None or self._runtime is None:
                self._pending_commands.append(command)
                return True
            loop = self._loop
            runtime = self._runtime
        try:
            loop.call_soon_threadsafe(runtime.handle, command)
            return True
        except RuntimeError:
            return False

    def publish(self, outcome: _Outcome) -> None:
        with self._lock:
            if self.finished.is_set():
                return
            if len(self._outcomes) >= MAX_READER_OUTCOMES:
                self._outcomes.popleft()
            self._outcomes.append(outcome)

    def take_outcomes(self) -> tuple[_Outcome, ...]:
        with self._lock:
            values = tuple(self._outcomes)
            self._outcomes.clear()
            return values

    def update_stats(
        self,
        *,
        pending: int,
        inflight: int,
        maximum_active: int,
        memory_cache_bytes: int,
    ) -> None:
        with self._lock:
            self.pending_page_count = pending
            self.inflight_page_count = inflight
            self.maximum_network_active = max(
                self.maximum_network_active,
                maximum_active,
            )
            self.memory_cache_bytes = memory_cache_bytes

    def mark_finished(self) -> None:
        with self._lock:
            self._loop = None
            self._runtime = None
            self._pending_commands.clear()
        self.finished.set()


class _PageScheduler:
    def __init__(self, runtime, capacity: int):
        self.runtime = runtime
        self.capacity = capacity
        self.condition = asyncio.Condition()
        self.pending: dict[
            tuple[int, ReaderContentMode, str, int, int], _PageJob
        ] = {}
        self.inflight: set[
            tuple[int, ReaderContentMode, str, int, int]
        ] = set()
        self.desired: set[
            tuple[int, ReaderContentMode, str, int, int]
        ] = set()
        self.pinned_cache_keys: frozenset[str] = frozenset()
        self.pinned_disk_page_keys: frozenset[str] = frozenset()
        self._sequence = 0
        self.stopped = False

    async def update_window(self, command: _WindowCommand) -> None:
        desired_specs = _desired_page_specs(command)
        async with self.condition:
            if self.stopped:
                return
            desired_keys = {
                (
                    command.generation,
                    command.content_mode,
                    command.photo_id,
                    page,
                    command.target_width,
                )
                for page, _priority in desired_specs
            }
            self.desired = desired_keys
            self.pinned_cache_keys = frozenset(
                (
                    f"{command.generation}:{command.content_mode.value}:"
                    f"{command.photo_id}:"
                    f"{page}:{command.target_width}"
                )
                for page in command.visible_pages
            )
            self.pinned_disk_page_keys = frozenset(
                f"{command.content_mode.value}:"
                f"{command.photo_id}:{page}"
                for page in command.visible_pages
            )
            self.pending = {
                key: job
                for key, job in self.pending.items()
                if key in desired_keys
            }
            for page, priority in desired_specs:
                self._sequence += 1
                job = _PageJob(
                    command.generation,
                    command.photo_id,
                    page,
                    command.total_pages,
                    command.current_page,
                    command.target_width,
                    priority,
                    self._sequence,
                    command.content_mode,
                )
                if job.key in self.inflight:
                    continue
                cached = self.runtime.memory_cache.get(job.cache_key)
                if cached is not None:
                    snapshot, image = cached
                    self.runtime.bridge.publish(
                        _Outcome(
                            job.generation,
                            "page_ready",
                            (snapshot, QImage(image)),
                        )
                    )
                    continue
                current = self.pending.get(job.key)
                if current is None or job.priority < current.priority:
                    self.pending[job.key] = job
            self._trim_pending()
            self.condition.notify_all()
            self._publish_stats()
        self.runtime.memory_cache.update_window(
            current_page=command.current_page,
            pinned_keys=self.pinned_cache_keys,
        )
        update_disk = getattr(
            self.runtime.service_for(command.content_mode),
            "update_cache_window",
            None,
        )
        if callable(update_disk):
            try:
                await asyncio.to_thread(
                    update_disk,
                    command.photo_id,
                    current_page=command.current_page,
                    visible_pages=command.visible_pages,
                )
            except Exception:
                pass

    async def retry(self, command: _RetryCommand) -> None:
        async with self.condition:
            if self.stopped:
                return
            for page in command.page_numbers:
                if not 1 <= page <= command.total_pages:
                    continue
                self._sequence += 1
                job = _PageJob(
                    command.generation,
                    command.photo_id,
                    page,
                    command.total_pages,
                    command.current_page,
                    command.target_width,
                    0,
                    self._sequence,
                    command.content_mode,
                )
                self.runtime.memory_cache.discard(job.cache_key)
                if job.key not in self.inflight:
                    self.pending[job.key] = job
                    self.desired.add(job.key)
            self._trim_pending()
            self.condition.notify_all()
            self._publish_stats()

    async def next_job(self) -> _PageJob | None:
        async with self.condition:
            while not self.pending and not self.stopped:
                await self.condition.wait()
            if self.stopped:
                return None
            key, job = min(
                self.pending.items(),
                key=lambda item: (
                    item[1].priority,
                    item[1].sequence,
                ),
            )
            self.pending.pop(key)
            self.inflight.add(key)
            self._publish_stats()
            return job

    async def complete(self, job: _PageJob) -> None:
        async with self.condition:
            self.inflight.discard(job.key)
            self._publish_stats()

    async def is_desired(self, job: _PageJob) -> bool:
        async with self.condition:
            return job.key in self.desired

    async def invalidate(self) -> None:
        async with self.condition:
            self.pending.clear()
            self.desired.clear()
            self.pinned_cache_keys = frozenset()
            self.pinned_disk_page_keys = frozenset()
            self.condition.notify_all()
            self._publish_stats()

    async def stop(self) -> None:
        async with self.condition:
            self.stopped = True
            self.pending.clear()
            self.desired.clear()
            self.pinned_cache_keys = frozenset()
            self.pinned_disk_page_keys = frozenset()
            self.condition.notify_all()
            self._publish_stats()

    def _trim_pending(self) -> None:
        if len(self.pending) <= self.capacity:
            return
        keep = sorted(
            self.pending.items(),
            key=lambda item: (
                item[1].priority,
                abs(item[1].page_number - item[1].current_page),
                item[1].sequence,
            ),
        )[: self.capacity]
        self.pending = dict(keep)

    def _publish_stats(self) -> None:
        self.runtime.bridge.update_stats(
            pending=len(self.pending),
            inflight=len(self.inflight),
            maximum_active=self.runtime.maximum_network_active,
            memory_cache_bytes=self.runtime.memory_cache.total_bytes,
        )


class _ReaderRuntime:
    def __init__(
        self,
        bridge: _ReaderBridge,
        online_service,
        local_service,
        history_store,
        memory_budget_bytes: int,
    ):
        self.bridge = bridge
        self.online_service = online_service
        self.local_service = local_service
        self.history_store = history_store
        self.memory_cache = ReaderMemoryCache[
            tuple[ReaderPageSnapshot, QImage]
        ](memory_budget_bytes)
        self.active_generation = 0
        self.active_content_mode = ReaderContentMode.ONLINE
        self.scheduler = _PageScheduler(self, MAX_READER_PAGE_QUEUE)
        self.network_semaphore = asyncio.Semaphore(
            MAX_READER_NETWORK_CONCURRENCY
        )
        self.network_active = 0
        self.maximum_network_active = 0
        self._workers = []
        self._tasks = set()
        self._history_tasks = set()
        self._stopping = False
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._workers = [
            asyncio.create_task(
                self._page_worker(),
                name=f"reader-page-{index + 1}",
            )
            for index in range(MAX_READER_NETWORK_CONCURRENCY)
        ]
        self.bridge.bind(loop, self)
        await self._stop_event.wait()

    def handle(self, command) -> None:
        if self._stopping:
            return
        if isinstance(command, _StopCommand):
            self._spawn(self._shutdown(command.timeout))
            return
        if isinstance(command, _LeaveCommand):
            self.active_generation = command.generation
            self._spawn(self.scheduler.invalidate())
            return
        if isinstance(command, _OpenCommand):
            self.active_generation = command.generation
            self.active_content_mode = command.content_mode
            self._spawn(self.scheduler.invalidate())
            self._spawn(self._load_catalog(command))
            return
        if isinstance(command, _ChapterCommand):
            self.active_generation = command.generation
            self.active_content_mode = command.content_mode
            self._spawn(self.scheduler.invalidate())
            self._spawn(self._load_chapter(command))
            return
        if isinstance(command, _WindowCommand):
            if command.generation == self.active_generation:
                self._spawn(self.scheduler.update_window(command))
            return
        if isinstance(command, _RetryCommand):
            if command.generation == self.active_generation:
                self._spawn(self.scheduler.retry(command))
            return
        if isinstance(command, _HistoryCommand):
            if self.history_store is not None:
                task = asyncio.create_task(self._save_history(command))
                self._history_tasks.add(task)
                task.add_done_callback(self._history_tasks.discard)

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _load_catalog(self, command: _OpenCommand) -> None:
        try:
            catalog = await self._call_service(
                command.content_mode,
                "fetch_catalog",
                command.album_id,
            )
            if command.generation == self.active_generation:
                self.bridge.publish(
                    _Outcome(
                        command.generation,
                        "catalog_ready",
                        (catalog,),
                    )
                )
        except Exception as error:
            self._publish_operation_error(command.generation, error)

    async def _load_chapter(self, command: _ChapterCommand) -> None:
        try:
            chapter, pages = await self._call_service(
                command.content_mode,
                "load_chapter",
                command.catalog,
                command.photo_id,
            )
            if command.generation != self.active_generation:
                return
            self.bridge.publish(
                _Outcome(
                    command.generation,
                    "chapter_ready",
                    (chapter, pages),
                )
            )
            await self.scheduler.update_window(
                _WindowCommand(
                    command.generation,
                    chapter.photo_id,
                    1,
                    (1,),
                    chapter.page_count,
                    command.target_width,
                    command.content_mode,
                )
            )
        except Exception as error:
            self._publish_operation_error(command.generation, error)

    async def _page_worker(self) -> None:
        while True:
            job = await self.scheduler.next_job()
            if job is None:
                return
            try:
                if (
                    job.generation != self.active_generation
                    or not await self.scheduler.is_desired(job)
                ):
                    continue
                self.bridge.publish(
                    _Outcome(
                        job.generation,
                        "page_loading",
                        (job.photo_id, job.page_number),
                    )
                )
                _disk_key, snapshot = await self._call_service(
                    job.content_mode,
                    "fetch_page",
                    job.photo_id,
                    job.page_number,
                    current_page=job.current_page,
                    pinned_keys=self.scheduler.pinned_disk_page_keys,
                )
                if (
                    job.generation != self.active_generation
                    or not await self.scheduler.is_desired(job)
                ):
                    continue
                image = await asyncio.to_thread(
                    _decode_reader_image,
                    snapshot.cache_path,
                    job.target_width,
                )
                if image is None or image.isNull():
                    raise ReaderServiceError(
                        ReaderErrorKind.IMAGE_DECODE_FAILED,
                        "图片无法交给阅读界面显示",
                    )
                try:
                    self.memory_cache.put(
                        job.cache_key,
                        (snapshot, QImage(image)),
                        byte_size=max(1, image.sizeInBytes()),
                        page_number=job.page_number,
                        current_page=job.current_page,
                        pinned_keys=self.scheduler.pinned_cache_keys,
                    )
                except ReaderCacheExhausted:
                    raise ReaderServiceError(
                        ReaderErrorKind.CACHE_EXHAUSTED,
                        "当前可见图片超过内存缓存上限",
                    ) from None
                if (
                    job.generation == self.active_generation
                    and await self.scheduler.is_desired(job)
                ):
                    self.bridge.publish(
                        _Outcome(
                            job.generation,
                            "page_ready",
                            (snapshot, image),
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if (
                    job.generation == self.active_generation
                    and await self.scheduler.is_desired(job)
                ):
                    kind, message = _safe_reader_error(error)
                    self.bridge.publish(
                        _Outcome(
                            job.generation,
                            "page_failed",
                            (job.photo_id, job.page_number),
                            kind,
                            message,
                        )
                    )
            finally:
                await self.scheduler.complete(job)

    async def _save_history(self, command: _HistoryCommand) -> None:
        entry = command.entry
        try:
            await asyncio.to_thread(
                self.history_store.record,
                album_id=entry.album_id,
                title=entry.title,
                photo_id=entry.photo_id,
                chapter_title=entry.chapter_title,
                chapter_index=entry.chapter_index,
                page_number=entry.page_number,
                page_count=entry.page_count,
                source=entry.source,
                content_mode=entry.content_mode,
            )
        except Exception:
            self.bridge.publish(
                _Outcome(
                    command.generation,
                    "history_failed",
                    error_kind=ReaderErrorKind.INTERNAL,
                    error_message="阅读进度暂时无法保存",
                )
            )

    async def _shutdown(self, timeout: float) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.active_generation += 1
        await self.scheduler.stop()
        if self._history_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *tuple(self._history_tasks),
                        return_exceptions=True,
                    ),
                    timeout=max(0.1, min(timeout, 2.0)),
                )
            except asyncio.TimeoutError:
                for task in tuple(self._history_tasks):
                    task.cancel()
        for task in tuple(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        services = tuple(
            dict.fromkeys(
                service
                for service in (self.online_service, self.local_service)
                if service is not None
            )
        )
        results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    service.close(),
                    timeout=max(0.1, timeout),
                )
                for service in services
            ),
            return_exceptions=True,
        )
        service_closed = all(value is True for value in results)
        self.memory_cache.clear()
        self.bridge.publish(
            _Outcome(
                self.active_generation,
                "shutdown",
                (bool(service_closed),),
            )
        )
        self._stop_event.set()

    def _publish_operation_error(self, generation: int, error) -> None:
        if generation != self.active_generation:
            return
        kind, message = _safe_reader_error(error)
        self.bridge.publish(
            _Outcome(
                generation,
                "operation_failed",
                error_kind=kind,
                error_message=message,
            )
        )

    def _network_enter(self) -> None:
        self.network_active += 1
        self.maximum_network_active = max(
            self.maximum_network_active,
            self.network_active,
        )
        self.scheduler._publish_stats()

    def _network_leave(self) -> None:
        self.network_active = max(0, self.network_active - 1)
        self.scheduler._publish_stats()

    def service_for(self, content_mode: ReaderContentMode):
        if content_mode is ReaderContentMode.ONLINE:
            return self.online_service
        if content_mode is ReaderContentMode.LOCAL and self.local_service:
            return self.local_service
        raise ReaderServiceError(
            ReaderErrorKind.CHAPTER_UNAVAILABLE,
            "本地阅读服务不可用",
        )

    async def _call_service(
        self,
        content_mode: ReaderContentMode,
        method_name: str,
        *args,
        **kwargs,
    ):
        service = self.service_for(content_mode)
        method = getattr(service, method_name)
        if content_mode is ReaderContentMode.LOCAL:
            return await method(*args, **kwargs)
        async with self.network_semaphore:
            self._network_enter()
            try:
                return await method(*args, **kwargs)
            finally:
                self._network_leave()


def _reader_thread(
    bridge: _ReaderBridge,
    online_service,
    local_service,
    history_store,
    memory_budget_bytes: int,
) -> None:
    try:
        asyncio.run(
            _ReaderRuntime(
                bridge,
                online_service,
                local_service,
                history_store,
                memory_budget_bytes,
            ).run()
        )
    except Exception:
        LOGGER.exception("Reader runtime failed")
    finally:
        bridge.mark_finished()


def _desired_page_specs(
    command: _WindowCommand,
) -> tuple[tuple[int, int], ...]:
    visible = tuple(
        sorted(
            {
                page
                for page in command.visible_pages
                if 1 <= page <= command.total_pages
            },
            key=lambda page: abs(page - command.current_page),
        )
    )
    values = []
    seen = set()
    for page in visible:
        if page not in seen:
            seen.add(page)
            values.append((page, 0))
    for page in range(
        command.current_page + 1,
        min(command.total_pages, command.current_page + 3) + 1,
    ):
        if page not in seen:
            seen.add(page)
            values.append((page, 1))
    for page in range(
        command.current_page - 1,
        max(1, command.current_page - 2) - 1,
        -1,
    ):
        if page not in seen:
            seen.add(page)
            values.append((page, 2))
    return tuple(values[:MAX_READER_PAGE_QUEUE])


def _decode_reader_image(
    path: Path | None,
    target_width: int,
) -> QImage | None:
    if (
        not isinstance(path, Path)
        or type(target_width) is not int
        or not 1 <= target_width <= MAX_READER_TARGET_WIDTH
    ):
        return None
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if (
        not size.isValid()
        or size.isEmpty()
        or size.width() > 16_384
        or size.height() > 16_384
        or size.width() * size.height() > 24_000_000
    ):
        return None
    if size.width() > target_width:
        reader.setScaledSize(
            size.scaled(
                QSize(target_width, MAX_READER_TARGET_WIDTH * 8),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
    image = reader.read()
    return image if not image.isNull() else None


def _safe_reader_error(
    error: Exception,
) -> tuple[ReaderErrorKind, str]:
    if isinstance(error, ReaderServiceError):
        return error.kind, str(error)
    return ReaderErrorKind.INTERNAL, "在线阅读暂时失败"


class ReaderController(QObject):
    catalog_ready = Signal(int, object)
    chapter_ready = Signal(int, object, object)
    page_loading = Signal(int, str, int)
    page_ready = Signal(int, object, QImage)
    page_failed = Signal(int, str, int, str, str)
    operation_failed = Signal(int, str, str)
    history_failed = Signal(str)
    shutdown_finished = Signal(bool)

    def __init__(
        self,
        service: ReaderService,
        *,
        local_service=None,
        history_store: ReaderHistoryStore | None = None,
        parent=None,
        result_interval_ms: int = DEFAULT_RESULT_INTERVAL_MS,
        history_debounce_ms: int = DEFAULT_HISTORY_DEBOUNCE_MS,
        memory_budget_bytes: int = 128 * 1024 * 1024,
    ):
        super().__init__(parent)
        if not all(
            callable(getattr(service, name, None))
            for name in (
                "fetch_catalog",
                "load_chapter",
                "fetch_page",
                "close",
            )
        ):
            raise TypeError("service does not implement reader operations")
        if local_service is not None and not all(
            callable(getattr(local_service, name, None))
            for name in (
                "fetch_catalog",
                "load_chapter",
                "fetch_page",
                "close",
            )
        ):
            raise TypeError(
                "local_service does not implement reader operations"
            )
        if history_store is not None and not isinstance(
            history_store,
            ReaderHistoryStore,
        ):
            raise TypeError("history_store must be ReaderHistoryStore")
        if type(result_interval_ms) is not int or result_interval_ms < 1:
            raise ValueError("result_interval_ms must be positive")
        if (
            type(history_debounce_ms) is not int
            or history_debounce_ms < 1
        ):
            raise ValueError("history_debounce_ms must be positive")
        if type(memory_budget_bytes) is not int or memory_budget_bytes < 1:
            raise ValueError("memory_budget_bytes must be positive")
        self.service = service
        self.local_service = local_service
        self.history_store = history_store
        self._bridge = _ReaderBridge()
        self._generation = 0
        self._disposed = False
        self._shutdown_requested = False
        self._history_context = None
        self._current_page = 0
        self._content_mode = ReaderContentMode.ONLINE

        self._thread = threading.Thread(
            target=_reader_thread,
            args=(
                self._bridge,
                service,
                local_service,
                history_store,
                memory_budget_bytes,
            ),
            name="jm-reader",
            daemon=True,
        )
        self._thread.start()

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(result_interval_ms)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(history_debounce_ms)
        self._history_timer.timeout.connect(self.flush_history)
        self.destroyed.connect(self._request_stop)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def pending_page_count(self) -> int:
        return self._bridge.pending_page_count

    @property
    def inflight_page_count(self) -> int:
        return self._bridge.inflight_page_count

    @property
    def maximum_network_active(self) -> int:
        return self._bridge.maximum_network_active

    @property
    def memory_cache_bytes(self) -> int:
        return self._bridge.memory_cache_bytes

    @property
    def worker_is_daemon(self) -> bool:
        return self._thread.daemon

    @property
    def content_mode(self) -> ReaderContentMode:
        return self._content_mode

    def open_album(
        self,
        album_id: str,
        *,
        content_mode: ReaderContentMode = ReaderContentMode.ONLINE,
    ) -> int:
        if self._disposed:
            return self._generation
        if not isinstance(content_mode, ReaderContentMode):
            raise TypeError("content_mode must be ReaderContentMode")
        self.flush_history()
        self._generation += 1
        self._history_context = None
        self._current_page = 0
        self._content_mode = content_mode
        self._bridge.submit(
            _OpenCommand(self._generation, album_id, content_mode)
        )
        return self._generation

    def load_chapter(
        self,
        catalog: ChapterCatalogSnapshot,
        photo_id: str,
        *,
        target_width: int,
        content_mode: ReaderContentMode | None = None,
    ) -> int:
        if self._disposed:
            return self._generation
        target_width = _validated_target_width(target_width)
        content_mode = content_mode or self._content_mode
        if not isinstance(content_mode, ReaderContentMode):
            raise TypeError("content_mode must be ReaderContentMode")
        self.flush_history()
        self._generation += 1
        self._history_context = None
        self._current_page = 0
        self._content_mode = content_mode
        self._bridge.submit(
            _ChapterCommand(
                self._generation,
                catalog,
                photo_id,
                target_width,
                content_mode,
            )
        )
        return self._generation

    def update_viewport(
        self,
        photo_id: str,
        *,
        current_page: int,
        visible_pages,
        total_pages: int,
        target_width: int,
    ) -> None:
        if self._disposed:
            return
        command = _validated_window_command(
            self._generation,
            photo_id,
            current_page,
            visible_pages,
            total_pages,
            target_width,
            self._content_mode,
        )
        self._current_page = current_page
        self._bridge.submit(command)
        if self._history_context is not None:
            self._history_timer.start()

    def retry_pages(
        self,
        photo_id: str,
        page_numbers,
        *,
        current_page: int,
        total_pages: int,
        target_width: int,
    ) -> None:
        if self._disposed:
            return
        values = tuple(
            sorted(
                {
                    page
                    for page in page_numbers
                    if type(page) is int and 1 <= page <= total_pages
                }
            )
        )
        if not values:
            return
        self._bridge.submit(
            _RetryCommand(
                self._generation,
                photo_id,
                values,
                current_page,
                total_pages,
                _validated_target_width(target_width),
                self._content_mode,
            )
        )

    def set_history_context(
        self,
        *,
        album_id: str,
        title: str,
        chapter: ReaderChapterSnapshot,
        source: ReaderSource,
        content_mode: ReaderContentMode | None = None,
    ) -> None:
        if (
            self._disposed
            or not isinstance(chapter, ReaderChapterSnapshot)
            or not isinstance(source, ReaderSource)
            or (
                content_mode is not None
                and not isinstance(content_mode, ReaderContentMode)
            )
        ):
            return
        self._history_context = (
            album_id,
            title,
            chapter,
            source,
            content_mode or self._content_mode,
        )

    @Slot()
    def flush_history(self) -> None:
        self._history_timer.stop()
        if (
            self._disposed
            or self.history_store is None
            or self._history_context is None
            or self._current_page < 1
        ):
            return
        album_id, title, chapter, source, content_mode = self._history_context
        page = min(self._current_page, chapter.page_count)
        entry = ReaderHistoryEntry(
            album_id,
            title,
            chapter.photo_id,
            chapter.title,
            chapter.index,
            page,
            chapter.page_count,
            _utc_now(),
            source,
            content_mode,
        )
        self._bridge.submit(
            _HistoryCommand(self._generation, entry)
        )

    def leave(self) -> int:
        if self._disposed:
            return self._generation
        self.flush_history()
        self._generation += 1
        self._history_context = None
        self._current_page = 0
        self._bridge.submit(_LeaveCommand(self._generation))
        return self._generation

    @Slot()
    def request_shutdown(self, timeout: float = 5.0) -> None:
        if self._shutdown_requested:
            return
        self.flush_history()
        self._shutdown_requested = True
        self._disposed = True
        self._history_timer.stop()
        self._bridge.submit(_StopCommand(max(0.1, float(timeout))))

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.request_shutdown(timeout)
        self._thread.join(max(0.0, timeout))
        self._drain_results()
        return not self._thread.is_alive()

    @Slot()
    def dispose(self) -> None:
        self.request_shutdown()

    @Slot()
    def _request_stop(self, *_args) -> None:
        if not self._shutdown_requested:
            self._bridge.submit(_StopCommand(5.0))

    @Slot()
    def _drain_results(self) -> None:
        for outcome in self._bridge.take_outcomes():
            if outcome.kind == "shutdown":
                self._result_timer.stop()
                self.shutdown_finished.emit(bool(outcome.payload[0]))
                continue
            if self._disposed or outcome.generation != self._generation:
                continue
            if outcome.kind == "catalog_ready":
                self.catalog_ready.emit(
                    outcome.generation,
                    outcome.payload[0],
                )
            elif outcome.kind == "chapter_ready":
                self.chapter_ready.emit(
                    outcome.generation,
                    outcome.payload[0],
                    outcome.payload[1],
                )
            elif outcome.kind == "page_loading":
                self.page_loading.emit(
                    outcome.generation,
                    outcome.payload[0],
                    outcome.payload[1],
                )
            elif outcome.kind == "page_ready":
                self.page_ready.emit(
                    outcome.generation,
                    outcome.payload[0],
                    QImage(outcome.payload[1]),
                )
            elif outcome.kind == "page_failed":
                self.page_failed.emit(
                    outcome.generation,
                    outcome.payload[0],
                    outcome.payload[1],
                    (outcome.error_kind or ReaderErrorKind.INTERNAL).value,
                    outcome.error_message or "图片加载失败",
                )
            elif outcome.kind == "operation_failed":
                self.operation_failed.emit(
                    outcome.generation,
                    (outcome.error_kind or ReaderErrorKind.INTERNAL).value,
                    outcome.error_message or "在线阅读暂时失败",
                )
            elif outcome.kind == "history_failed":
                self.history_failed.emit(
                    outcome.error_message or "阅读进度暂时无法保存"
                )


def _validated_target_width(value: int) -> int:
    if (
        type(value) is not int
        or not 1 <= value <= MAX_READER_TARGET_WIDTH
    ):
        raise ValueError("target_width is out of range")
    return value


def _validated_window_command(
    generation: int,
    photo_id: str,
    current_page: int,
    visible_pages,
    total_pages: int,
    target_width: int,
    content_mode: ReaderContentMode = ReaderContentMode.ONLINE,
) -> _WindowCommand:
    if not isinstance(content_mode, ReaderContentMode):
        raise TypeError("content_mode must be ReaderContentMode")
    if (
        not isinstance(photo_id, str)
        or not photo_id
        or type(total_pages) is not int
        or not 1 <= total_pages <= MAX_READER_CHAPTER_PAGES
        or type(current_page) is not int
        or not 1 <= current_page <= total_pages
    ):
        raise ValueError("reader viewport is invalid")
    visible = tuple(
        sorted(
            {
                page
                for page in visible_pages
                if type(page) is int and 1 <= page <= total_pages
            }
        )
    )
    if not visible:
        visible = (current_page,)
    return _WindowCommand(
        generation,
        photo_id,
        current_page,
        visible,
        total_pages,
        _validated_target_width(target_width),
        content_mode,
    )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_HISTORY_DEBOUNCE_MS",
    "MAX_READER_NETWORK_CONCURRENCY",
    "MAX_READER_PAGE_QUEUE",
    "ReaderController",
]
