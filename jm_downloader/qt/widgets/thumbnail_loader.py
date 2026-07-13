import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QImageReader


@dataclass(frozen=True, slots=True)
class _ThumbnailKey:
    task_id: str
    revision: int
    resolved_path: str
    modified_ns: int
    file_size: int
    target_width: int
    target_height: int


@dataclass(frozen=True, slots=True)
class _RequestToken:
    key: _ThumbnailKey
    task_generation: int


class _WorkerSignals(QObject):
    finished = Signal(object, QImage)


class _ThumbnailRunnable(QRunnable):
    def __init__(self, token: _RequestToken, target_size: QSize):
        super().__init__()
        self.token = token
        self.target_size = QSize(target_size)
        self.signals = _WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            image = self._read_image()
        except Exception:
            image = QImage()
        self.signals.finished.emit(self.token, image)

    def _read_image(self) -> QImage:
        reader = QImageReader(self.token.key.resolved_path)
        reader.setAutoTransform(True)

        if self.target_size.width() > 0 and self.target_size.height() > 0:
            source_size = reader.size()
            scaled_size = (
                source_size.scaled(
                    self.target_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                )
                if source_size.isValid()
                else self.target_size
            )
            reader.setScaledSize(scaled_size)

        return reader.read()


class ThumbnailLoader(QObject):
    thumbnail_ready = Signal(str, int, QImage)
    _ready = Signal(object, QImage)

    def __init__(self, parent=None, thread_pool: QThreadPool | None = None):
        super().__init__(parent)
        self._thread_pool = thread_pool or QThreadPool(self)
        self._lock = threading.RLock()
        self._cache: dict[_ThumbnailKey, QImage] = {}
        self._inflight: set[_RequestToken] = set()
        self._workers: dict[_RequestToken, _ThumbnailRunnable] = {}
        self._task_generations: dict[str, int] = {}
        self._ready.connect(
            self._deliver,
            Qt.ConnectionType.QueuedConnection,
        )

    def request(
        self,
        task_id: str,
        revision: int,
        path: Path,
        target_size: QSize,
    ) -> None:
        """Queue a thumbnail request, or deliver a cached result asynchronously."""
        key = self._make_key(task_id, revision, path, target_size)
        with self._lock:
            generation = self._task_generations.get(key.task_id, 0)
            token = _RequestToken(key, generation)
            cached = self._cache.get(key)
            if cached is not None:
                self._ready.emit(token, cached.copy())
                return
            if token in self._inflight:
                return

            worker = _ThumbnailRunnable(token, target_size)
            worker.signals.finished.connect(
                self._handle_finished,
                Qt.ConnectionType.QueuedConnection,
            )
            self._inflight.add(token)
            self._workers[token] = worker

        self._thread_pool.start(worker)

    def clear_task(self, task_id: str) -> None:
        task_id = str(task_id)
        with self._lock:
            self._task_generations[task_id] = (
                self._task_generations.get(task_id, 0) + 1
            )
            self._cache = {
                key: image
                for key, image in self._cache.items()
                if key.task_id != task_id
            }
            self._inflight = {
                token
                for token in self._inflight
                if token.key.task_id != task_id
            }

    @staticmethod
    def _make_key(
        task_id: str,
        revision: int,
        path: Path,
        target_size: QSize,
    ) -> _ThumbnailKey:
        resolved = Path(path).resolve()
        try:
            stat = resolved.stat()
            modified_ns = stat.st_mtime_ns
            file_size = stat.st_size
        except OSError:
            modified_ns = -1
            file_size = -1

        return _ThumbnailKey(
            task_id=str(task_id),
            revision=int(revision),
            resolved_path=str(resolved),
            modified_ns=modified_ns,
            file_size=file_size,
            target_width=target_size.width(),
            target_height=target_size.height(),
        )

    @Slot(object, QImage)
    def _handle_finished(self, token: _RequestToken, image: QImage) -> None:
        with self._lock:
            self._workers.pop(token, None)
            self._inflight.discard(token)
            current_generation = self._task_generations.get(token.key.task_id, 0)
            if token.task_generation != current_generation:
                return
            self._cache[token.key] = image.copy()
        self._ready.emit(token, image)

    @Slot(object, QImage)
    def _deliver(self, token: _RequestToken, image: QImage) -> None:
        with self._lock:
            current_generation = self._task_generations.get(token.key.task_id, 0)
        if token.task_generation != current_generation:
            return
        self.thumbnail_ready.emit(token.key.task_id, token.key.revision, image)
