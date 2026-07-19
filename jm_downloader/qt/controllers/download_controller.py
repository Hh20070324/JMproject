import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...library import LibraryError, LibraryNotFound, LibraryService
from ...models import TaskSnapshot, TaskStatus
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
        self._shutting_down = False
        self._cleanup_lock = threading.Lock()
        self._delete_intents = {}
        self._cleanup_threads = set()
        self._reported_store_error = None

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(max(1, event_interval_ms))
        self._event_timer.timeout.connect(self._drain_events)
        self._event_timer.start()

        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setInterval(max(50, reconcile_interval_ms))
        self._reconcile_timer.timeout.connect(self._reconcile)
        self._reconcile_timer.start()

        self._preview_thread = threading.Thread(
            target=self._restore_previews,
            name="task-preview-restore",
            daemon=True,
        )
        self._preview_thread.start()

    def list_tasks(self) -> list[TaskSnapshot]:
        return self.manager.list_tasks()

    @Slot(str)
    @Slot(str, object)
    def add_task(
        self,
        album_id: str,
        selected_chapter_ids=None,
    ) -> TaskSnapshot | None:
        try:
            if selected_chapter_ids is None:
                created = self.manager.add(album_id)
            else:
                created = self.manager.add(
                    album_id,
                    selected_chapter_ids=selected_chapter_ids,
                )
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
    def pause_task(self, task_id: str) -> None:
        try:
            self.manager.pause(task_id)
        except TaskError as error:
            self.command_failed.emit("pause", str(error))
            return
        self._publish(force=True)

    @Slot(str)
    def resume_task(self, task_id: str) -> None:
        try:
            self.manager.resume(task_id)
        except TaskError as error:
            self.command_failed.emit("resume", str(error))
            return
        self._publish(force=True)

    @Slot(str, bool)
    def cancel_task(self, task_id: str, delete_files: bool = False) -> None:
        try:
            if delete_files:
                snapshot = self.manager.get_task(task_id)
                paths = self.manager.get_task_paths(task_id)
                with self._cleanup_lock:
                    self._delete_intents[task_id] = (
                        snapshot.album_id,
                        paths,
                    )
                try:
                    self.manager.prepare_cancel(task_id)
                except Exception:
                    with self._cleanup_lock:
                        self._delete_intents.pop(task_id, None)
                    raise
            else:
                self.manager.cancel(task_id)
        except TaskError as error:
            self.command_failed.emit("cancel", str(error))
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

    @Slot(str, str)
    def open_task_item(self, task_id: str, kind: str) -> None:
        try:
            snapshot = self.manager.get_task(task_id)
            paths = self.manager.get_task_paths(task_id)
            LibraryService(paths).open_location(snapshot.album_id, kind)
        except (TaskError, LibraryError, OSError) as error:
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

            self._shutting_down = True
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
        self._shutting_down = True
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
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            background_finished = self._wait_background_threads(deadline)
            manager_finished = self.manager.shutdown(
                timeout=max(0.0, deadline - time.monotonic())
            )
            return background_finished and manager_finished
        except Exception:
            LOGGER.exception("Download manager shutdown failed")
            return False

    @Slot()
    def _drain_events(self) -> None:
        received = False
        while True:
            try:
                event = self._listener.get_nowait()
                received = True
                if event.get("type") == "cancel_ready":
                    self._start_delete_cleanup(event.get("id"))
            except queue.Empty:
                break
        if received:
            self._publish()

    @Slot()
    def _reconcile(self) -> None:
        with self._cleanup_lock:
            pending_delete_ids = tuple(self._delete_intents)
        for task_id in pending_delete_ids:
            if self.manager.is_cancel_ready(task_id):
                self._start_delete_cleanup(task_id)
        store_error = self.manager.task_store.last_error
        if store_error is not None and store_error is not self._reported_store_error:
            self._reported_store_error = store_error
            self.command_failed.emit(
                "store",
                "任务恢复记录暂时无法保存，请检查程序目录权限和磁盘空间",
            )
        self._publish()

    def _publish(self, force: bool = False) -> None:
        current = tuple(self.manager.list_tasks())
        if not force and current == self._last_tasks:
            return
        self._last_tasks = current
        self.tasks_reset.emit(list(current))

    def _restore_previews(self) -> None:
        for snapshot in tuple(self._last_tasks):
            if self._shutting_down:
                return
            if snapshot.preview_path is not None or snapshot.status not in (
                TaskStatus.PAUSED,
                TaskStatus.FAILED,
            ):
                continue
            try:
                self.manager.restore_preview(snapshot.id)
            except (TaskError, OSError):
                continue

    def _start_delete_cleanup(self, task_id: str | None) -> None:
        if not task_id or self._shutting_down:
            return
        with self._cleanup_lock:
            intent = self._delete_intents.pop(task_id, None)
            if intent is None:
                return
            thread = threading.Thread(
                target=self._delete_cancelled_task,
                args=(task_id, *intent),
                name=f"task-delete-{task_id}",
                daemon=True,
            )
            self._cleanup_threads.add(thread)
        thread.start()

    def _delete_cancelled_task(self, task_id, album_id, paths) -> None:
        error = None
        try:
            try:
                LibraryService(paths).delete_all(album_id)
            except LibraryNotFound:
                pass
        except (LibraryError, OSError) as caught:
            error = caught

        try:
            if self._shutting_down:
                return
            if error is None:
                self.manager.finish_cancel(task_id)
            else:
                message = f"删除下载文件失败：{error}"
                self.manager.fail_cancel(task_id, message)
                self.command_failed.emit("cancel", message)
        except (TaskError, OSError):
            if not self._shutting_down:
                LOGGER.exception("Failed to finalize task cancellation")
        finally:
            current = threading.current_thread()
            with self._cleanup_lock:
                self._cleanup_threads.discard(current)

    def _wait_background_threads(self, deadline: float) -> bool:
        threads = [self._preview_thread]
        with self._cleanup_lock:
            threads.extend(self._cleanup_threads)
        all_finished = True
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            all_finished = not thread.is_alive() and all_finished
        return all_finished
