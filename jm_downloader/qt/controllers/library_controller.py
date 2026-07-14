import logging
import queue
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot

from ...library import LibraryError, LibraryService
from ...models import LibraryItem, TaskStatus
from ...tasks import TaskError, TaskManager


LOGGER = logging.getLogger("jm-downloader")


class _LibraryWorkerSignals(QObject):
    finished = Signal(int, str, str, object, object)


class _LibraryRunnable(QRunnable):
    def __init__(
        self,
        request_id: int,
        command: str,
        album_id: str,
        function: Callable[[], object],
    ):
        super().__init__()
        self.request_id = request_id
        self.command = command
        self.album_id = album_id
        self.function = function
        self.signals = _LibraryWorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.function()
            error = None
        except Exception as caught:
            result = None
            error = caught
        self.signals.finished.emit(
            self.request_id,
            self.command,
            self.album_id,
            result,
            error,
        )


class LibraryController(QObject):
    items_reset = Signal(object)
    loading_changed = Signal(bool)
    busy_albums_changed = Signal(object)
    active_albums_changed = Signal(object)
    operation_succeeded = Signal(str, str)
    command_failed = Signal(str, str, str)

    MUTATION_COMMANDS = {
        "rebuild",
        "delete_images",
        "delete_pdf",
        "delete_all",
    }
    ACTIVE_STATUSES = {
        TaskStatus.PENDING,
        TaskStatus.FETCHING,
        TaskStatus.DOWNLOADING,
        TaskStatus.PAUSING,
        TaskStatus.PAUSED,
        TaskStatus.CANCELLING,
        TaskStatus.FAILED,
    }

    def __init__(
        self,
        manager: TaskManager,
        library: LibraryService,
        parent=None,
        thread_pool: QThreadPool | None = None,
        event_interval_ms: int = 50,
        reconcile_interval_ms: int = 1000,
    ):
        super().__init__(parent)
        self.manager = manager
        self.library = library
        self._thread_pool = thread_pool or QThreadPool(self)
        self._thread_pool.setMaxThreadCount(2)
        self._listener = self.manager.add_listener()
        self._workers = {}
        self._items: tuple[LibraryItem, ...] = ()
        self._busy_albums = set()
        self._busy_lock = threading.Lock()
        (
            self._active_albums,
            self._terminal_task_signature,
        ) = self._read_task_state()
        self._request_id = 0
        self._requested_scan_id = 0
        self._scan_running = False
        self._refresh_pending = False
        self._loading = False
        self._disposed = False

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(max(1, event_interval_ms))
        self._event_timer.timeout.connect(self._drain_task_events)
        self._event_timer.start()

        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setInterval(max(50, reconcile_interval_ms))
        self._reconcile_timer.timeout.connect(self._reconcile_active_albums)
        self._reconcile_timer.start()

    def list_items(self) -> list[LibraryItem]:
        return list(self._items)

    def active_album_ids(self) -> frozenset[str]:
        return self._active_albums

    def busy_album_ids(self) -> frozenset[str]:
        with self._busy_lock:
            return frozenset(self._busy_albums)

    def has_pending_mutations(self) -> bool:
        with self._busy_lock:
            return bool(self._busy_albums)

    @Slot()
    def refresh(self) -> None:
        if self._disposed:
            return
        scan_id = self._next_request_id()
        self._requested_scan_id = scan_id
        if self._scan_running:
            self._refresh_pending = True
            self._set_loading(True)
            return
        self._start_scan(scan_id)

    @Slot(str, str)
    def open_item(self, album_id: str, kind: str) -> None:
        try:
            self.library.open_location(album_id, kind)
        except (LibraryError, OSError) as error:
            self.command_failed.emit("open", str(album_id), str(error))

    @Slot(str)
    def rebuild_pdf(self, album_id: str) -> None:
        self._start_mutation("rebuild", album_id)

    @Slot(str, str)
    def delete_item(self, album_id: str, kind: str) -> None:
        command = {
            "images": "delete_images",
            "pdf": "delete_pdf",
            "all": "delete_all",
        }.get(str(kind))
        if command is None:
            self.command_failed.emit(
                "delete",
                str(album_id),
                "不支持的删除类型",
            )
            return
        self._start_mutation(command, album_id)

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.dispose()
        return self._thread_pool.waitForDone(int(max(0.0, timeout) * 1000))

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._event_timer.stop()
        self._reconcile_timer.stop()
        self.manager.remove_listener(self._listener)

    def _start_scan(self, scan_id: int) -> None:
        self._scan_running = True
        self._refresh_pending = False
        self._set_loading(True)
        try:
            self._submit(scan_id, "refresh", "", self.library.list_items)
        except Exception as error:
            self._scan_running = False
            self._set_loading(False)
            self._report_error("refresh", "", error)

    def _start_mutation(self, command: str, album_id: str) -> None:
        if self._disposed:
            return
        album_id = str(album_id)
        try:
            album_id = self.manager.begin_library_operation(album_id)
        except TaskError as error:
            self.command_failed.emit(command, album_id, str(error))
            return

        with self._busy_lock:
            self._busy_albums.add(album_id)
            busy = frozenset(self._busy_albums)
        self.busy_albums_changed.emit(busy)

        # Any scan already in flight predates this mutation and must not win later.
        self._requested_scan_id = self._next_request_id()
        self._refresh_pending = False

        method = {
            "rebuild": self.library.rebuild_pdf,
            "delete_images": self.library.delete_images,
            "delete_pdf": self.library.delete_pdf,
            "delete_all": self.library.delete_all,
        }[command]

        def execute():
            try:
                return method(album_id)
            finally:
                self.manager.end_library_operation(album_id)

        request_id = self._next_request_id()
        try:
            self._submit(request_id, command, album_id, execute)
        except Exception as error:
            self.manager.end_library_operation(album_id)
            with self._busy_lock:
                self._busy_albums.discard(album_id)
                busy = frozenset(self._busy_albums)
            self.busy_albums_changed.emit(busy)
            self._report_error(command, album_id, error)

    def _submit(
        self,
        request_id: int,
        command: str,
        album_id: str,
        function: Callable[[], object],
    ) -> None:
        worker = _LibraryRunnable(request_id, command, album_id, function)
        worker.signals.finished.connect(
            self._handle_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._workers[request_id] = worker
        try:
            self._thread_pool.start(worker)
        except Exception:
            self._workers.pop(request_id, None)
            raise

    @Slot(int, str, str, object, object)
    def _handle_finished(
        self,
        request_id: int,
        command: str,
        album_id: str,
        result,
        error,
    ) -> None:
        self._workers.pop(request_id, None)
        if self._disposed:
            return
        if command == "refresh":
            self._finish_scan(request_id, result, error)
            return

        with self._busy_lock:
            self._busy_albums.discard(album_id)
            busy = frozenset(self._busy_albums)
        self.busy_albums_changed.emit(busy)

        if error is not None:
            self._report_error(command, album_id, error)
        else:
            self.operation_succeeded.emit(command, album_id)
        self.refresh()

    def _finish_scan(self, request_id: int, result, error) -> None:
        self._scan_running = False
        if request_id == self._requested_scan_id:
            if error is not None:
                self._report_error("refresh", "", error)
            else:
                self._items = tuple(result)
                self.items_reset.emit(list(self._items))

        if self._refresh_pending:
            self._start_scan(self._requested_scan_id)
        else:
            self._set_loading(False)

    def _report_error(self, command: str, album_id: str, error: Exception) -> None:
        if not isinstance(error, (LibraryError, TaskError, OSError)):
            LOGGER.error(
                "Library command failed: %s %s",
                command,
                album_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        self.command_failed.emit(command, album_id, str(error) or "操作失败")

    def _set_loading(self, loading: bool) -> None:
        loading = bool(loading)
        if loading == self._loading:
            return
        self._loading = loading
        self.loading_changed.emit(loading)

    @Slot()
    def _drain_task_events(self) -> None:
        refresh_needed = False
        received = False
        while True:
            try:
                event = self._listener.get_nowait()
                received = True
                if event.get("type") in {"completed", "failed"}:
                    refresh_needed = True
            except queue.Empty:
                break
        state_requires_refresh = self._publish_task_state() if received else False
        if refresh_needed or state_requires_refresh:
            self.refresh()

    @Slot()
    def _reconcile_active_albums(self) -> None:
        if self._publish_task_state():
            self.refresh()

    def _publish_task_state(self) -> bool:
        active_albums, terminal_signature = self._read_task_state()
        became_inactive = bool(self._active_albums - active_albums)
        terminal_changed = terminal_signature != self._terminal_task_signature
        if active_albums != self._active_albums:
            self._active_albums = active_albums
            self.active_albums_changed.emit(active_albums)
        self._terminal_task_signature = terminal_signature
        return became_inactive or terminal_changed

    def _read_task_state(self) -> tuple[frozenset[str], tuple]:
        tasks = self.manager.list_tasks()
        active_albums = frozenset(
            task.album_id
            for task in tasks
            if task.status in self.ACTIVE_STATUSES
        )
        terminal_signature = tuple(
            (
                task.id,
                task.album_id,
                task.status.value,
                str(task.pdf_path or ""),
                task.error or "",
            )
            for task in tasks
            if task.status not in self.ACTIVE_STATUSES
        )
        return active_albums, terminal_signature

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id
