import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QSize,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QImageReader

from ...search import MAX_COVER_BYTES, SearchService
from ...tasks import InvalidAlbumId, normalize_album_id


DEFAULT_WORKER_COUNT = 4
DEFAULT_CACHE_CAPACITY = 128
DEFAULT_QUEUE_CAPACITY = 64
DEFAULT_RESULT_INTERVAL_MS = 15
MAX_WORKER_COUNT = 4
MAX_SOURCE_DIMENSION = 8_192
MAX_SOURCE_PIXELS = 16_777_216
MAX_TARGET_DIMENSION = 512
MAX_TARGET_PIXELS = 262_144
MAX_CACHE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _CoverKey:
    album_id: str
    target_width: int
    target_height: int


@dataclass(frozen=True, slots=True)
class _CoverResult:
    key: _CoverKey
    image: QImage | None


@dataclass(frozen=True, slots=True)
class _CachedDelivery:
    generation: int
    key: _CoverKey
    image: QImage


class _CoverMailbox:
    """Thread-safe queues and stop state with no QObject dependency."""

    def __init__(
        self,
        service: SearchService,
        queue_capacity: int,
        result_capacity: int,
    ):
        self.service = service
        self.pending: queue.Queue[_CoverKey] = queue.Queue(queue_capacity)
        self.completed: queue.Queue[_CoverResult | _CachedDelivery] = queue.Queue(
            result_capacity
        )
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def submit(self, key: _CoverKey) -> bool:
        with self.lock:
            if self.stopped.is_set():
                return False
            try:
                self.pending.put_nowait(key)
            except queue.Full:
                return False
            return True

    def publish(self, outcome: _CoverResult | _CachedDelivery) -> bool:
        with self.lock:
            if self.stopped.is_set():
                return False
            try:
                self.completed.put_nowait(outcome)
            except queue.Full:
                return False
            return True

    def close(self, *_args) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            while True:
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    break
            while True:
                try:
                    self.completed.get_nowait()
                except queue.Empty:
                    break


def _cover_worker(mailbox: _CoverMailbox) -> None:
    while not mailbox.stopped.is_set():
        try:
            key = mailbox.pending.get(timeout=0.1)
        except queue.Empty:
            continue

        if mailbox.stopped.is_set():
            continue
        try:
            content = mailbox.service.fetch_cover(key.album_id)
            image = _decode_cover(content, key)
        except Exception:
            image = None

        mailbox.publish(_CoverResult(key, image))


def _decode_cover(content: object, key: _CoverKey) -> QImage | None:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        return None
    content = bytes(content)
    if not content or len(content) > MAX_COVER_BYTES:
        return None

    buffer = QBuffer()
    buffer.setData(content)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None

    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if not _source_size_is_safe(source_size):
        return None

    target_size = QSize(key.target_width, key.target_height)
    scaled_size = source_size.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
    )
    if not scaled_size.isValid() or scaled_size.isEmpty():
        return None
    reader.setScaledSize(scaled_size)

    image = reader.read()
    if image.isNull():
        return None
    if (
        image.width() > target_size.width()
        or image.height() > target_size.height()
    ):
        image = image.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image if not image.isNull() else None


def _source_size_is_safe(size: QSize) -> bool:
    if not size.isValid() or size.isEmpty():
        return False
    width = size.width()
    height = size.height()
    return (
        width <= MAX_SOURCE_DIMENSION
        and height <= MAX_SOURCE_DIMENSION
        and width * height <= MAX_SOURCE_PIXELS
    )


class SearchCoverLoader(QObject):
    cover_ready = Signal(int, str, QImage)
    cover_failed = Signal(int, str)

    def __init__(
        self,
        service: SearchService,
        parent=None,
        worker_count: int = DEFAULT_WORKER_COUNT,
        cache_capacity: int = DEFAULT_CACHE_CAPACITY,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ):
        super().__init__(parent)
        if not hasattr(service, "fetch_cover") or not callable(
            service.fetch_cover
        ):
            raise TypeError("service must provide fetch_cover(album_id)")
        if (
            type(worker_count) is not int
            or not 1 <= worker_count <= MAX_WORKER_COUNT
        ):
            raise ValueError("worker_count must be between 1 and 4")
        if type(cache_capacity) is not int or cache_capacity < 1:
            raise ValueError("cache_capacity must be a positive integer")
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")

        self._outstanding_capacity = worker_count + queue_capacity
        self._delivery_capacity = queue_capacity
        self._listener_capacity = max(2, queue_capacity)
        self._mailbox = _CoverMailbox(
            service,
            queue_capacity,
            self._outstanding_capacity + self._delivery_capacity,
        )
        self._cache_capacity = cache_capacity
        self._cache: OrderedDict[_CoverKey, QImage] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[_CoverKey, OrderedDict[int, None]] = {}
        self._cached_deliveries: set[tuple[int, _CoverKey]] = set()
        self._lock = threading.RLock()
        self._disposed = False

        workers = []
        try:
            for index in range(worker_count):
                worker = threading.Thread(
                    target=_cover_worker,
                    args=(self._mailbox,),
                    name=f"jm-search-cover-{index + 1}",
                    daemon=True,
                )
                worker.start()
                workers.append(worker)
        except Exception:
            self._mailbox.close()
            raise
        self._workers = tuple(workers)

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(DEFAULT_RESULT_INTERVAL_MS)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self.destroyed.connect(self._mailbox.close)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def workers_are_daemon(self) -> bool:
        return all(worker.daemon for worker in self._workers)

    @property
    def pending_request_count(self) -> int:
        return self._mailbox.pending.qsize()

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def request(
        self,
        generation: int,
        album_id: str,
        target_size: QSize,
    ) -> bool:
        if self._disposed or type(generation) is not int or generation < 0:
            return False
        key = self._make_key(album_id, target_size)
        if key is None:
            return False

        with self._lock:
            if self._disposed:
                return False
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                delivery_key = (generation, key)
                if delivery_key in self._cached_deliveries:
                    return True
                if len(self._cached_deliveries) >= self._delivery_capacity:
                    return False
                self._cached_deliveries.add(delivery_key)
                if not self._mailbox.publish(
                    _CachedDelivery(generation, key, QImage(cached))
                ):
                    self._cached_deliveries.discard(delivery_key)
                    return False
                return True

            listeners = self._inflight.get(key)
            if listeners is not None:
                if generation in listeners:
                    return True
                if len(listeners) >= self._listener_capacity:
                    return False
                listeners.setdefault(generation, None)
                return True

            if len(self._inflight) >= self._outstanding_capacity:
                return False
            listeners = OrderedDict(((generation, None),))
            self._inflight[key] = listeners
            if not self._mailbox.submit(key):
                self._inflight.pop(key, None)
                return False
        return True

    @staticmethod
    def _make_key(album_id: str, target_size: QSize) -> _CoverKey | None:
        if not isinstance(album_id, str) or not isinstance(target_size, QSize):
            return None
        try:
            album_id = normalize_album_id(album_id)
        except InvalidAlbumId:
            return None

        width = target_size.width()
        height = target_size.height()
        if (
            width < 1
            or height < 1
            or width > MAX_TARGET_DIMENSION
            or height > MAX_TARGET_DIMENSION
            or width * height > MAX_TARGET_PIXELS
        ):
            return None
        return _CoverKey(album_id, width, height)

    @Slot()
    def _drain_results(self) -> None:
        while True:
            try:
                outcome = self._mailbox.completed.get_nowait()
            except queue.Empty:
                return

            if isinstance(outcome, _CachedDelivery):
                with self._lock:
                    self._cached_deliveries.discard(
                        (outcome.generation, outcome.key)
                    )
                    if self._disposed:
                        continue
                self.cover_ready.emit(
                    outcome.generation,
                    outcome.key.album_id,
                    QImage(outcome.image),
                )
                continue

            with self._lock:
                listeners = self._inflight.pop(outcome.key, OrderedDict())
                if self._disposed:
                    continue
                if outcome.image is not None:
                    self._remember(outcome.key, outcome.image)

            for generation in listeners:
                if self._disposed:
                    return
                if outcome.image is None:
                    self.cover_failed.emit(generation, outcome.key.album_id)
                else:
                    self.cover_ready.emit(
                        generation,
                        outcome.key.album_id,
                        QImage(outcome.image),
                    )

    def _remember(self, key: _CoverKey, image: QImage) -> None:
        image_bytes = max(0, image.sizeInBytes())
        if image_bytes > MAX_CACHE_BYTES:
            return
        replaced = self._cache.pop(key, None)
        if replaced is not None:
            self._cache_bytes -= max(0, replaced.sizeInBytes())
        self._cache[key] = QImage(image)
        self._cache_bytes += image_bytes
        while (
            len(self._cache) > self._cache_capacity
            or self._cache_bytes > MAX_CACHE_BYTES
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= max(0, evicted.sizeInBytes())

    @Slot()
    def dispose(self) -> None:
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self._result_timer.stop()
            self._cache.clear()
            self._cache_bytes = 0
            self._inflight.clear()
            self._cached_deliveries.clear()
        self._mailbox.close()


__all__ = ["SearchCoverLoader"]
