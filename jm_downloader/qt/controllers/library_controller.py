import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot

from ...library import LibraryError, LibraryService
from ...models import (
    ChapterRebuildResult,
    ChapterRepairPlan,
    LegacyMigrationPlan,
    LibraryItem,
    TaskConfig,
    TaskStatus,
)
from ...tasks import TaskError, TaskManager


LOGGER = logging.getLogger("jm-downloader")


@dataclass(frozen=True, slots=True)
class BatchDeleteResult:
    kind: str
    succeeded: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ChapterRepairExecutionResult:
    album_id: str
    plan: ChapterRepairPlan
    rebuild_result: ChapterRebuildResult | None
    created_tasks: tuple[object, ...]
    task_error: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedChapterRepair:
    plan: ChapterRepairPlan
    rebuild_result: ChapterRebuildResult | None
    base_config: TaskConfig


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
    operation_completed = Signal(str, str, object)
    request_completed = Signal(int, str, str, object)
    request_failed = Signal(int, str, str, str)
    command_failed = Signal(str, str, str)
    batch_delete_finished = Signal(str, object, object)

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
        self._batch_albums = {}
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

    @Slot(str, str, str, object)
    def delete_chapter(
        self,
        album_id: str,
        photo_id: str,
        kind: str,
        expected=None,
    ) -> int | None:
        kind = str(kind)
        command = {
            "images": "delete_chapter_images",
            "package": "delete_chapter_package",
            "all": "delete_chapter_all",
        }.get(kind)
        if command is None:
            self.command_failed.emit(
                "delete_chapter",
                str(album_id),
                "不支持的章节删除类型",
            )
            return None
        return self._start_mutation(
            command,
            album_id,
            function=lambda reserved_album_id: self.library.delete_chapter(
                reserved_album_id,
                str(photo_id),
                kind,
                expected=expected,
            ),
        )

    @Slot(str, result=object)
    def check_chapters(self, album_id: str) -> int | None:
        return self._start_mutation(
            "check_chapters",
            album_id,
            function=lambda reserved_album_id: self.library.check_chapters(
                reserved_album_id
            ),
        )

    @Slot(str, result=object)
    def probe_local_read(self, album_id: str) -> int | None:
        return self._start_read_probe(
            album_id,
            function=lambda reserved_album_id: self.library.probe_local_read(
                reserved_album_id
            ),
        )

    @Slot(str, object, object, result=object)
    def rebuild_chapters(
        self,
        album_id: str,
        photo_ids,
        confirmed_formats=None,
    ) -> int | None:
        selected = tuple(str(value) for value in photo_ids)
        choices = dict(confirmed_formats or {})
        return self._start_mutation(
            "rebuild_chapters",
            album_id,
            function=lambda reserved_album_id: self.library.rebuild_chapters(
                reserved_album_id,
                selected,
                confirmed_formats=choices,
            ),
        )

    @Slot(str, object, object, object, result=object)
    def repair_chapters(
        self,
        album_id: str,
        photo_ids,
        confirmed_formats,
        base_config,
    ) -> int | None:
        selected = tuple(str(value) for value in photo_ids)
        choices = dict(confirmed_formats or {})
        if not isinstance(base_config, TaskConfig):
            self.command_failed.emit(
                "repair_chapters",
                str(album_id),
                "当前下载设置无效",
            )
            return None
        base_config.validate()

        def execute(reserved_album_id: str):
            plan = self.library.plan_chapter_repairs(
                reserved_album_id,
                selected,
                confirmed_formats=choices,
            )
            rebuild_result = None
            if plan.rebuild_photo_ids:
                rebuild_result = self.library.rebuild_chapters(
                    reserved_album_id,
                    plan.rebuild_photo_ids,
                    confirmed_formats=dict(plan.resolved_formats),
                )
            return _PreparedChapterRepair(
                plan=plan,
                rebuild_result=rebuild_result,
                base_config=base_config,
            )

        return self._start_mutation(
            "repair_chapters",
            album_id,
            function=execute,
        )

    @Slot(str, object, result=object)
    def plan_legacy_migration(
        self,
        album_id: str,
        catalog,
    ) -> int | None:
        return self._start_mutation(
            "plan_legacy_migration",
            album_id,
            function=lambda reserved_album_id: (
                self.library.plan_legacy_migration(
                    reserved_album_id,
                    catalog,
                )
            ),
        )

    @Slot(object, result=object)
    def migrate_legacy_layout(
        self,
        plan: LegacyMigrationPlan,
    ) -> int | None:
        if not isinstance(plan, LegacyMigrationPlan):
            self.command_failed.emit(
                "migrate_legacy_layout",
                "",
                "迁移方案无效",
            )
            return None
        return self._start_mutation(
            "migrate_legacy_layout",
            plan.album_id,
            function=lambda _reserved_album_id: (
                self.library.migrate_legacy_layout(plan)
            ),
        )

    @Slot(object, str)
    def batch_delete(self, album_ids, kind: str) -> bool:
        if self._disposed:
            return False
        command = {
            "images": "delete_images",
            "pdf": "delete_pdf",
            "all": "delete_all",
        }.get(str(kind))
        if command is None:
            self.command_failed.emit(
                "batch_delete",
                "",
                "不支持的删除类型",
            )
            return False

        normalized = []
        seen = set()
        for value in album_ids:
            album_id = str(value)
            if album_id in seen:
                continue
            seen.add(album_id)
            normalized.append(album_id)
        if not normalized:
            self.command_failed.emit(
                "batch_delete",
                "",
                "请先选择要删除的漫画",
            )
            return False

        reserved = []
        failures = []
        for album_id in normalized:
            try:
                reserved.append(
                    self.manager.begin_library_operation(album_id)
                )
            except TaskError as error:
                failures.append((album_id, str(error)))

        if not reserved:
            self.batch_delete_finished.emit(
                str(kind),
                (),
                tuple(failures),
            )
            return False

        with self._busy_lock:
            self._busy_albums.update(reserved)
            busy = frozenset(self._busy_albums)
        self.busy_albums_changed.emit(busy)

        # An older scan must not republish entries removed by this batch.
        self._requested_scan_id = self._next_request_id()
        self._refresh_pending = False
        method = {
            "delete_images": self.library.delete_images,
            "delete_pdf": self.library.delete_pdf,
            "delete_all": self.library.delete_all,
        }[command]

        def execute():
            succeeded = []
            current_failures = list(failures)
            for album_id in reserved:
                try:
                    method(album_id)
                except Exception as error:
                    current_failures.append(
                        (
                            album_id,
                            self._batch_error_message(
                                command,
                                album_id,
                                error,
                            ),
                        )
                    )
                else:
                    succeeded.append(album_id)
                finally:
                    self.manager.end_library_operation(album_id)
            return BatchDeleteResult(
                str(kind),
                tuple(succeeded),
                tuple(current_failures),
            )

        request_id = self._next_request_id()
        self._batch_albums[request_id] = tuple(reserved)
        try:
            self._submit(
                request_id,
                f"batch_{command}",
                "",
                execute,
            )
        except Exception as error:
            self._batch_albums.pop(request_id, None)
            for album_id in reserved:
                self.manager.end_library_operation(album_id)
            with self._busy_lock:
                self._busy_albums.difference_update(reserved)
                busy = frozenset(self._busy_albums)
            self.busy_albums_changed.emit(busy)
            self._report_error("batch_delete", "", error)
            return False
        return True

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

    def _start_mutation(
        self,
        command: str,
        album_id: str,
        *,
        function: Callable[[str], object] | None = None,
    ) -> int | None:
        if self._disposed:
            return None
        album_id = str(album_id)
        request_id = self._next_request_id()
        try:
            album_id = self.manager.begin_library_operation(album_id)
        except TaskError as error:
            message = str(error)
            self.command_failed.emit(command, album_id, message)
            QTimer.singleShot(
                0,
                lambda: self.request_failed.emit(
                    request_id,
                    command,
                    album_id,
                    message,
                ),
            )
            return request_id

        with self._busy_lock:
            self._busy_albums.add(album_id)
            busy = frozenset(self._busy_albums)
        self.busy_albums_changed.emit(busy)

        # Any scan already in flight predates this mutation and must not win later.
        self._requested_scan_id = self._next_request_id()
        self._refresh_pending = False

        if function is None:
            method = {
                "delete_images": self.library.delete_images,
                "delete_pdf": self.library.delete_pdf,
                "delete_all": self.library.delete_all,
            }[command]
        else:
            method = function

        def execute():
            try:
                return method(album_id)
            finally:
                self.manager.end_library_operation(album_id)

        try:
            self._submit(request_id, command, album_id, execute)
        except Exception as error:
            self.manager.end_library_operation(album_id)
            with self._busy_lock:
                self._busy_albums.discard(album_id)
                busy = frozenset(self._busy_albums)
            self.busy_albums_changed.emit(busy)
            self._report_error(
                command,
                album_id,
                error,
                request_id=request_id,
            )
        return request_id

    def _start_read_probe(
        self,
        album_id: str,
        *,
        function: Callable[[str], object],
    ) -> int | None:
        if self._disposed:
            return None
        command = "probe_local_read"
        album_id = str(album_id)
        request_id = self._next_request_id()
        try:
            album_id = self.manager.begin_library_operation(album_id)
        except TaskError as error:
            message = str(error) or "本地章节检查未能启动"
            QTimer.singleShot(
                0,
                lambda: self.request_failed.emit(
                    request_id,
                    command,
                    album_id,
                    message,
                ),
            )
            return request_id

        def execute():
            try:
                return function(album_id)
            finally:
                self.manager.end_library_operation(album_id)

        try:
            self._submit(request_id, command, album_id, execute)
        except Exception as error:
            self.manager.end_library_operation(album_id)
            self._report_error(
                command,
                album_id,
                error,
                request_id=request_id,
            )
        return request_id

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
        if command == "probe_local_read":
            if error is not None:
                self._report_error(
                    command,
                    album_id,
                    error,
                    request_id=request_id,
                )
            else:
                self.request_completed.emit(
                    request_id,
                    command,
                    album_id,
                    result,
                )
            return
        if command.startswith("batch_delete_"):
            albums = self._batch_albums.pop(request_id, ())
            with self._busy_lock:
                self._busy_albums.difference_update(albums)
                busy = frozenset(self._busy_albums)
            self.busy_albums_changed.emit(busy)
            if error is not None:
                for item in albums:
                    self.manager.end_library_operation(item)
                self._report_error("batch_delete", "", error)
            else:
                self.batch_delete_finished.emit(
                    result.kind,
                    result.succeeded,
                    result.failures,
                )
            self.refresh()
            return

        with self._busy_lock:
            self._busy_albums.discard(album_id)
            busy = frozenset(self._busy_albums)
        self.busy_albums_changed.emit(busy)

        if error is not None:
            self._report_error(
                command,
                album_id,
                error,
                request_id=request_id,
            )
        else:
            if (
                command == "repair_chapters"
                and isinstance(result, _PreparedChapterRepair)
            ):
                try:
                    created = self.manager.add_repair_batches(
                        album_id,
                        result.plan.download_batches,
                        base_config=result.base_config,
                    )
                    task_error = None
                except TaskError as caught:
                    created = ()
                    task_error = str(caught) or "无法创建重新下载任务"
                result = ChapterRepairExecutionResult(
                    album_id=album_id,
                    plan=result.plan,
                    rebuild_result=result.rebuild_result,
                    created_tasks=tuple(created),
                    task_error=task_error,
                )
            self.operation_succeeded.emit(command, album_id)
            self.operation_completed.emit(command, album_id, result)
            self.request_completed.emit(
                request_id,
                command,
                album_id,
                result,
            )
        if command not in {"check_chapters", "plan_legacy_migration"}:
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

    def _report_error(
        self,
        command: str,
        album_id: str,
        error: Exception,
        *,
        request_id: int | None = None,
    ) -> None:
        if not isinstance(error, (LibraryError, TaskError, OSError)):
            LOGGER.error(
                "Library command failed: %s %s",
                command,
                album_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        message = str(error) or "操作失败"
        self.command_failed.emit(command, album_id, message)
        if request_id is not None:
            QTimer.singleShot(
                0,
                lambda: self.request_failed.emit(
                    request_id,
                    command,
                    album_id,
                    message,
                ),
            )

    @staticmethod
    def _batch_error_message(
        command: str,
        album_id: str,
        error: Exception,
    ) -> str:
        if isinstance(error, (LibraryError, TaskError, OSError)):
            return str(error) or "操作失败"
        LOGGER.error(
            "Library batch command failed: %s %s",
            command,
            album_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return "操作失败"

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
                str(task.pdf_directory or ""),
                task.error or "",
            )
            for task in tasks
            if task.status not in self.ACTIVE_STATUSES
        )
        return active_albums, terminal_signature

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id
