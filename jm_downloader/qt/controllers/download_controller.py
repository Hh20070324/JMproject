import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...library import LibraryError, LibraryService
from ...models import TaskSnapshot
from ...tasks import TaskError, TaskManager


LOGGER = logging.getLogger("jm-downloader")


class DownloadController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)
    shutdown_finished = Signal(bool)

    def __init__(
        self,
        manager: TaskManager,
        library: LibraryService,
        parent=None,
        event_interval_ms: int = 50,
        reconcile_interval_ms: int = 1000,
    ):
        super().__init__(parent)
        self.manager = manager
        self.library = library
        self._listener = self.manager.add_listener()
        self._last_tasks = tuple(self.manager.list_tasks())
        self._disposed = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_thread = None
        self._shutdown_result = None

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(max(1, event_interval_ms))
        self._event_timer.timeout.connect(self._drain_events)
        self._event_timer.start()

        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setInterval(max(50, reconcile_interval_ms))
        self._reconcile_timer.timeout.connect(self._reconcile)
        self._reconcile_timer.start()

    def list_tasks(self) -> list[TaskSnapshot]:
        return self.manager.list_tasks()

    @Slot(str)
    def add_task(self, album_id: str) -> TaskSnapshot | None:
        try:
            created = self.manager.add(album_id)
            snapshot = self.manager.get_task(created.id)
        except TaskError as error:
            self.command_failed.emit("add", str(error))
            return None
        self._publish(force=True)
        return snapshot

    @Slot(str)
    def retry_task(self, task_id: str) -> None:
        try:
            self.manager.retry(task_id)
        except TaskError as error:
            self.command_failed.emit("retry", str(error))
            return
        self._publish(force=True)

    @Slot(str)
    def remove_task(self, task_id: str) -> None:
        try:
            self.manager.remove(task_id)
        except TaskError as error:
            self.command_failed.emit("remove", str(error))
            return
        self._publish(force=True)

    @Slot(str, str)
    def open_item(self, album_id: str, kind: str) -> None:
        try:
            self.library.open_location(album_id, kind)
        except (LibraryError, OSError) as error:
            self.command_failed.emit("open", str(error))

    def has_active_tasks(self) -> bool:
        return self.manager.has_active_tasks()

    def stop_all(self) -> None:
        self.manager.stop_all()

    def begin_shutdown(self, timeout: float = 5.0) -> None:
        with self._shutdown_lock:
            thread = self._shutdown_thread
            if thread is not None and thread.is_alive():
                return
            if self._shutdown_result is True:
                QTimer.singleShot(0, lambda: self.shutdown_finished.emit(True))
                return

            self.dispose()
            thread = threading.Thread(
                target=self._run_shutdown,
                args=(timeout,),
                name="qt-download-shutdown",
                daemon=True,
            )
            self._shutdown_thread = thread
            thread.start()

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.dispose()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._shutdown_lock:
            thread = self._shutdown_thread

        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(max(0.0, deadline - time.monotonic()))

        with self._shutdown_lock:
            thread = self._shutdown_thread
            cached = self._shutdown_result
        if thread is not None and thread.is_alive():
            return False
        if cached is True:
            return True

        result = self._shutdown_manager(max(0.0, deadline - time.monotonic()))
        with self._shutdown_lock:
            self._shutdown_result = result
        return result

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._event_timer.stop()
        self._reconcile_timer.stop()
        self.manager.remove_listener(self._listener)

    def _run_shutdown(self, timeout: float) -> None:
        result = self._shutdown_manager(timeout)
        with self._shutdown_lock:
            self._shutdown_result = result
        self.shutdown_finished.emit(result)

    def _shutdown_manager(self, timeout: float) -> bool:
        try:
            return self.manager.shutdown(timeout=max(0.0, timeout))
        except Exception:
            LOGGER.exception("Download manager shutdown failed")
            return False

    @Slot()
    def _drain_events(self) -> None:
        received = False
        while True:
            try:
                self._listener.get_nowait()
                received = True
            except queue.Empty:
                break
        if received:
            self._publish()

    @Slot()
    def _reconcile(self) -> None:
        self._publish()

    def _publish(self, force: bool = False) -> None:
        current = tuple(self.manager.list_tasks())
        if not force and current == self._last_tasks:
            return
        self._last_tasks = current
        self.tasks_reset.emit(list(current))
