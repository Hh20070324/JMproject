import logging
import threading
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...favorites import (
    FavoritesAccountMismatch,
    FavoritesAddUncertain,
    FavoritesDeletePreflightFailed,
    FavoritesError,
    FavoritesFolderExists,
    FavoritesFolderNotEmpty,
    FavoritesFolderProtected,
    FavoritesInvalidAlbumId,
    FavoritesInvalidFolderName,
    FavoritesLocalDataError,
    FavoritesMutationUncertain,
    FavoritesOperationCancelled,
    FavoritesResponseError,
    FavoritesService,
    FavoritesSessionExpired,
    FavoritesSessionRequired,
    FavoritesSnapshotRequired,
    FavoritesStorageError,
    FavoritesToggleRemoved,
    FavoritesUnavailable,
)
from ...models import (
    AccountSnapshot,
    AccountStatus,
    FavoriteFolderSnapshot,
    FavoritesFilterSnapshot,
    FavoritesSnapshot,
    FavoritesSyncProgress,
)
from .account_controller import AccountController


LOGGER = logging.getLogger("jm-downloader")
DEFAULT_RESULT_INTERVAL_MS = 15


@dataclass(frozen=True, slots=True)
class _FavoritesJob:
    generation: int
    operation: int
    command: str
    album_id: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    order_by: str = "mr"


@dataclass(frozen=True, slots=True)
class _FavoritesOutcome:
    job: _FavoritesJob
    snapshot: FavoritesSnapshot | None = None
    album_id: str | None = None
    mutation_value: str | None = None
    remote_changed: bool = False
    partial_code: str | None = None
    partial_message: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _FavoritesProgress:
    job: _FavoritesJob
    progress: FavoritesSyncProgress


@dataclass(frozen=True, slots=True)
class _FilterJob:
    generation: int
    folder: FavoriteFolderSnapshot
    keyword: str


@dataclass(frozen=True, slots=True)
class _FilterOutcome:
    job: _FilterJob
    snapshot: FavoritesFilterSnapshot


class _FavoritesMailbox:
    def __init__(self, service: FavoritesService):
        self.service = service
        self.condition = threading.Condition()
        self.pending: _FavoritesJob | None = None
        self.completed: deque[_FavoritesOutcome] = deque()
        self.progress: deque[_FavoritesProgress] = deque(maxlen=1)
        self.latest_generation = 0
        self.stopped = False

    def submit(self, job: _FavoritesJob) -> bool:
        with self.condition:
            if self.stopped:
                return False
            self.latest_generation = job.generation
            self.pending = job
            self.completed.clear()
            self.progress.clear()
            self.condition.notify()
            return True

    def next_job(self) -> _FavoritesJob | None:
        with self.condition:
            while self.pending is None and not self.stopped:
                self.condition.wait()
            if self.stopped:
                return None
            job = self.pending
            self.pending = None
            return job

    def publish_progress(
        self,
        job: _FavoritesJob,
        progress: FavoritesSyncProgress,
    ) -> None:
        with self.condition:
            if (
                self.stopped
                or job.generation != self.latest_generation
            ):
                return
            self.progress.append(_FavoritesProgress(job, progress))

    def publish(self, outcome: _FavoritesOutcome) -> None:
        with self.condition:
            if (
                self.stopped
                or outcome.job.generation != self.latest_generation
            ):
                return
            self.completed.append(outcome)

    def take_results(
        self,
    ) -> tuple[tuple[_FavoritesProgress, ...], tuple[_FavoritesOutcome, ...]]:
        with self.condition:
            progress = tuple(self.progress)
            completed = tuple(self.completed)
            self.progress.clear()
            self.completed.clear()
            return progress, completed

    def invalidate(self, generation: int) -> None:
        with self.condition:
            if self.stopped:
                return
            self.latest_generation = generation
            self.pending = None
            self.progress.clear()
            self.completed.clear()

    def close(self, *_args) -> None:
        with self.condition:
            if self.stopped:
                return
            self.stopped = True
            self.latest_generation += 1
            self.pending = None
            self.progress.clear()
            self.completed.clear()
            self.condition.notify_all()


class _FilterMailbox:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending: _FilterJob | None = None
        self.completed: deque[_FilterOutcome] = deque(maxlen=1)
        self.latest_generation = 0
        self.stopped = False

    def submit(self, job: _FilterJob) -> bool:
        with self.condition:
            if self.stopped:
                return False
            self.latest_generation = job.generation
            self.pending = job
            self.completed.clear()
            self.condition.notify()
            return True

    def next_job(self) -> _FilterJob | None:
        with self.condition:
            while self.pending is None and not self.stopped:
                self.condition.wait()
            if self.stopped:
                return None
            job = self.pending
            self.pending = None
            return job

    def publish(self, outcome: _FilterOutcome) -> None:
        with self.condition:
            if (
                self.stopped
                or outcome.job.generation != self.latest_generation
            ):
                return
            self.completed.append(outcome)

    def take_results(self) -> tuple[_FilterOutcome, ...]:
        with self.condition:
            completed = tuple(self.completed)
            self.completed.clear()
            return completed

    def invalidate(self, generation: int) -> None:
        with self.condition:
            if self.stopped:
                return
            self.latest_generation = generation
            self.pending = None
            self.completed.clear()

    def close(self, *_args) -> None:
        with self.condition:
            if self.stopped:
                return
            self.stopped = True
            self.latest_generation += 1
            self.pending = None
            self.completed.clear()
            self.condition.notify_all()


def _favorites_worker(mailbox: _FavoritesMailbox) -> None:
    while True:
        job = mailbox.next_job()
        if job is None:
            return
        remote_changed = False
        album_id = job.album_id
        mutation_value = None
        partial_code = None
        partial_message = None
        try:
            if job.command == "restore":
                snapshot = mailbox.service.restore(job.operation)
            elif job.command == "sync":
                snapshot = mailbox.service.sync(
                    job.operation,
                    lambda progress: mailbox.publish_progress(job, progress),
                    order_by=job.order_by,
                )
            elif job.command == "add":
                album_id = mailbox.service.add_album(
                    job.album_id or "",
                    job.operation,
                )
                mutation_value = album_id
                remote_changed = True
                if job.folder_id not in {None, "0"}:
                    try:
                        mailbox.service.move_album(
                            album_id,
                            job.folder_id,
                            job.operation,
                        )
                    except FavoritesOperationCancelled:
                        raise
                    except Exception as error:
                        partial_code, partial_message = (
                            _partial_move_error_payload(error)
                        )
                snapshot = mailbox.service.sync(
                    job.operation,
                    lambda progress: mailbox.publish_progress(job, progress),
                    order_by=job.order_by,
                )
            elif job.command == "create_folder":
                mutation_value = mailbox.service.create_folder(
                    job.folder_name or "",
                    job.operation,
                )
                remote_changed = True
                snapshot = mailbox.service.sync(
                    job.operation,
                    lambda progress: mailbox.publish_progress(job, progress),
                    order_by=job.order_by,
                )
            elif job.command == "delete_folder":
                mutation_value = mailbox.service.delete_folder(
                    job.folder_id or "",
                    job.operation,
                )
                remote_changed = True
                snapshot = mailbox.service.sync(
                    job.operation,
                    lambda progress: mailbox.publish_progress(job, progress),
                    order_by=job.order_by,
                )
            elif job.command == "move_album":
                mutation_value = mailbox.service.move_album(
                    job.album_id or "",
                    job.folder_id or "",
                    job.operation,
                )
                remote_changed = True
                snapshot = mailbox.service.sync(
                    job.operation,
                    lambda progress: mailbox.publish_progress(job, progress),
                    order_by=job.order_by,
                )
            else:
                raise FavoritesError()
            outcome = _FavoritesOutcome(
                job,
                snapshot=snapshot,
                album_id=album_id,
                mutation_value=mutation_value,
                remote_changed=remote_changed,
                partial_code=partial_code,
                partial_message=partial_message,
            )
        except Exception as error:
            code, message = _safe_error_payload(error)
            if code != FavoritesOperationCancelled.code:
                LOGGER.warning(
                    "Favorites worker failed: generation=%s command=%s "
                    "category=%s error_type=%s",
                    job.generation,
                    job.command,
                    code,
                    type(error).__name__,
                )
            outcome = _FavoritesOutcome(
                job,
                snapshot=(
                    mailbox.service.snapshot
                    if remote_changed or job.command != "add"
                    else None
                ),
                album_id=album_id,
                mutation_value=mutation_value,
                remote_changed=remote_changed,
                partial_code=partial_code,
                partial_message=partial_message,
                error_code=code,
                error_message=message,
            )
        mailbox.publish(outcome)


def _filter_worker(mailbox: _FilterMailbox) -> None:
    while True:
        job = mailbox.next_job()
        if job is None:
            return
        snapshot = _filter_folder_items(job.folder, job.keyword)
        mailbox.publish(_FilterOutcome(job, snapshot))


def _safe_error_payload(error: Exception) -> tuple[str, str]:
    for error_type in (
        FavoritesSessionRequired,
        FavoritesSessionExpired,
        FavoritesAccountMismatch,
        FavoritesToggleRemoved,
        FavoritesAddUncertain,
        FavoritesMutationUncertain,
        FavoritesDeletePreflightFailed,
        FavoritesSnapshotRequired,
        FavoritesInvalidFolderName,
        FavoritesFolderExists,
        FavoritesFolderNotEmpty,
        FavoritesFolderProtected,
        FavoritesInvalidAlbumId,
        FavoritesLocalDataError,
        FavoritesUnavailable,
        FavoritesResponseError,
        FavoritesStorageError,
        FavoritesOperationCancelled,
    ):
        if isinstance(error, error_type):
            return error_type.code, error_type.default_message
    return FavoritesError.code, FavoritesError.default_message


class FavoritesController(QObject):
    snapshot_changed = Signal(object)
    progress_changed = Signal(object)
    operation_failed = Signal(str, str)
    busy_changed = Signal(bool, str)
    add_succeeded = Signal(str)
    add_failed = Signal(str, str, str)
    add_partially_succeeded = Signal(str, str, str)
    mutation_succeeded = Signal(str, str)
    mutation_failed = Signal(str, str, str)
    mutation_refresh_failed = Signal(str, str, str)
    filter_result_changed = Signal(int, object)
    filter_busy_changed = Signal(bool)
    add_availability_changed = Signal(bool)
    known_favorite_ids_changed = Signal(object)

    def __init__(
        self,
        service: FavoritesService,
        account_controller: AccountController,
        parent=None,
        result_interval_ms: int = DEFAULT_RESULT_INTERVAL_MS,
    ):
        super().__init__(parent)
        if not isinstance(service, FavoritesService):
            raise TypeError("service must be FavoritesService")
        if not isinstance(account_controller, AccountController):
            raise TypeError("account_controller must be AccountController")
        if type(result_interval_ms) is not int or result_interval_ms < 1:
            raise ValueError("result_interval_ms must be a positive integer")
        self.service = service
        self.account_controller = account_controller
        self._mailbox = _FavoritesMailbox(service)
        self._filter_mailbox = _FilterMailbox()
        self._generation = 0
        self._filter_generation = 0
        self._snapshot = service.snapshot
        self._busy = False
        self._command = ""
        self._disposed = False
        self._account_snapshot = account_controller.current_snapshot
        self._known_favorite_ids = _favorite_ids(self._snapshot)
        self._add_available = self._calculate_add_available()
        self._filter_snapshot: FavoritesFilterSnapshot | None = None
        self._filter_busy = False

        self._worker = threading.Thread(
            target=_favorites_worker,
            args=(self._mailbox,),
            name="jm-favorites",
            daemon=True,
        )
        self._worker.start()
        self._filter_worker = threading.Thread(
            target=_filter_worker,
            args=(self._filter_mailbox,),
            name="jm-favorites-filter",
            daemon=True,
        )
        self._filter_worker.start()

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(result_interval_ms)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self.destroyed.connect(self._mailbox.close)
        self.destroyed.connect(self._filter_mailbox.close)

        account_controller.snapshot_changed.connect(
            self._on_account_snapshot
        )
        account_controller.operation_completed.connect(
            self._on_account_operation_completed
        )
        QTimer.singleShot(
            0,
            lambda: self._on_account_snapshot(
                self.account_controller.current_snapshot
            ),
        )

    @property
    def current_snapshot(self) -> FavoritesSnapshot | None:
        return self._snapshot

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def current_command(self) -> str:
        return self._command

    @property
    def can_add_favorites(self) -> bool:
        return self._add_available

    @property
    def known_favorite_ids(self) -> frozenset[str]:
        return self._known_favorite_ids

    @property
    def worker_is_daemon(self) -> bool:
        return self._worker.daemon

    @property
    def filter_worker_is_daemon(self) -> bool:
        return self._filter_worker.daemon

    @property
    def current_filter(self) -> FavoritesFilterSnapshot | None:
        return self._filter_snapshot

    @property
    def is_filter_busy(self) -> bool:
        return self._filter_busy

    @Slot()
    def restore(self) -> int | None:
        return self._submit("restore")

    @Slot()
    @Slot(str)
    def sync(self, order_by: str | None = None) -> int | None:
        order_by = _safe_order_by(order_by, self._snapshot)
        if order_by is None:
            return None
        return self._submit("sync", order_by=order_by)

    @Slot(str)
    @Slot(str, str)
    def add_album(
        self,
        album_id: str,
        folder_id: str = "0",
    ) -> int | None:
        album_id = _safe_album_id(album_id)
        if album_id is None:
            self.add_failed.emit(
                "",
                FavoritesInvalidAlbumId.code,
                FavoritesInvalidAlbumId.default_message,
            )
            return None
        if not self._add_available or album_id in self._known_favorite_ids:
            return None
        folder_id = _safe_target_folder_id(folder_id, self._snapshot)
        if folder_id is None:
            return None
        return self._submit(
            "add",
            album_id=album_id,
            folder_id=folder_id,
            order_by=_snapshot_order(self._snapshot),
        )

    @Slot(str)
    def create_folder(self, name: str) -> int | None:
        if not isinstance(name, str):
            return None
        return self._submit(
            "create_folder",
            folder_name=name,
            order_by=_snapshot_order(self._snapshot),
        )

    @Slot(str)
    def delete_folder(self, folder_id: str) -> int | None:
        folder_id = _safe_existing_folder_id(
            folder_id,
            self._snapshot,
            allow_all=False,
        )
        if folder_id is None:
            return None
        return self._submit(
            "delete_folder",
            folder_id=folder_id,
            order_by=_snapshot_order(self._snapshot),
        )

    @Slot(str, str)
    def move_album(self, album_id: str, folder_id: str) -> int | None:
        album_id = _safe_album_id(album_id)
        folder_id = _safe_existing_folder_id(
            folder_id,
            self._snapshot,
            allow_all=True,
        )
        if album_id is None or folder_id is None:
            return None
        return self._submit(
            "move_album",
            album_id=album_id,
            folder_id=folder_id,
            order_by=_snapshot_order(self._snapshot),
        )

    @Slot(str, str)
    def filter_items(self, folder_id: str, keyword: str) -> int | None:
        if self._disposed or not isinstance(keyword, str):
            return None
        folder = _folder_by_id(self._snapshot, folder_id)
        if folder is None:
            return None
        normalized = _normalize_filter_text(keyword)
        self._filter_generation += 1
        job = _FilterJob(self._filter_generation, folder, normalized)
        if not self._filter_mailbox.submit(job):
            return None
        self._set_filter_busy(True)
        return job.generation

    @Slot()
    def cancel_sync(self) -> None:
        if self._disposed or not self._busy or self._command != "sync":
            return
        self.service.cancel_operations()
        self._generation += 1
        self._mailbox.invalidate(self._generation)
        self.progress_changed.emit(None)
        self._set_busy(False, "")
        self.operation_failed.emit(
            FavoritesOperationCancelled.code,
            FavoritesOperationCancelled.default_message,
        )

    @Slot()
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.service.cancel_operations()
        self._result_timer.stop()
        self._mailbox.close()
        self._filter_mailbox.close()
        self._busy = False
        self._command = ""
        self._known_favorite_ids = frozenset()
        self._set_add_available(False)
        self._filter_generation += 1
        self._filter_snapshot = None
        self._set_filter_busy(False)

    def _submit(
        self,
        command: str,
        *,
        album_id: str | None = None,
        folder_id: str | None = None,
        folder_name: str | None = None,
        order_by: str = "mr",
    ) -> int | None:
        if self._disposed or self._busy:
            return None
        operation = self.service.start_operation()
        self._generation += 1
        job = _FavoritesJob(
            self._generation,
            operation,
            command,
            album_id,
            folder_id,
            folder_name,
            order_by,
        )
        if not self._mailbox.submit(job):
            return None
        self.progress_changed.emit(None)
        self._set_busy(True, command)
        return job.generation

    @Slot()
    def _drain_results(self) -> None:
        if self._disposed:
            return
        progress_items, outcomes = self._mailbox.take_results()
        for item in progress_items:
            if item.job.generation == self._generation:
                self.progress_changed.emit(item.progress)
        for outcome in outcomes:
            if outcome.job.generation != self._generation:
                continue
            self._set_busy(False, "")
            self.progress_changed.emit(None)
            if (
                outcome.snapshot is not None
                and outcome.snapshot is not self._snapshot
            ):
                self._publish_snapshot(
                    outcome.snapshot,
                    rebuild_known=outcome.error_code is None,
                )
            if outcome.error_code == FavoritesOperationCancelled.code:
                continue
            if outcome.error_code is not None:
                message = (
                    outcome.error_message or FavoritesError.default_message
                )
                if outcome.remote_changed:
                    self.mutation_refresh_failed.emit(
                        outcome.job.command,
                        outcome.error_code,
                        "远端修改已成功，但本地收藏刷新失败，请手动同步",
                    )
                elif outcome.job.command == "add":
                    self.add_failed.emit(
                        outcome.album_id or "",
                        outcome.error_code,
                        message,
                    )
                elif outcome.job.command in {
                    "create_folder",
                    "delete_folder",
                    "move_album",
                }:
                    self.mutation_failed.emit(
                        outcome.job.command,
                        outcome.error_code,
                        message,
                    )
                else:
                    self.operation_failed.emit(outcome.error_code, message)
            if outcome.remote_changed and outcome.job.command == "add":
                album_id = outcome.album_id or ""
                self._publish_known_favorite_ids(
                    self._known_favorite_ids | {album_id}
                )
                if outcome.partial_code is not None:
                    self.add_partially_succeeded.emit(
                        album_id,
                        outcome.partial_code,
                        outcome.partial_message or "已收藏，但移动失败",
                    )
                else:
                    self.add_succeeded.emit(album_id)
            elif outcome.remote_changed:
                self.mutation_succeeded.emit(
                    outcome.job.command,
                    outcome.mutation_value or "",
                )
            if outcome.job.command in {
                "sync",
                "add",
                "create_folder",
                "delete_folder",
                "move_album",
            }:
                self.account_controller.refresh_snapshot()
        for outcome in self._filter_mailbox.take_results():
            if outcome.job.generation != self._filter_generation:
                continue
            self._filter_snapshot = outcome.snapshot
            self._set_filter_busy(False)
            self.filter_result_changed.emit(
                outcome.job.generation,
                outcome.snapshot,
            )

    @Slot(object)
    def _on_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        if self._disposed or not isinstance(snapshot, AccountSnapshot):
            return
        self._account_snapshot = snapshot
        self._set_add_available(self._calculate_add_available())
        if snapshot.status in {
            AccountStatus.SIGNED_OUT,
            AccountStatus.RESTORING,
            AccountStatus.LOCAL_DATA_UNREADABLE,
        }:
            self._cancel_for_account_change(
                clear=True,
                clear_runtime_additions=True,
            )
            return
        if snapshot.status is AccountStatus.SIGNING_IN:
            self._cancel_for_account_change(
                clear=False,
                clear_runtime_additions=True,
                clear_known=True,
            )
            return
        if snapshot.status in {
            AccountStatus.SAVED_SESSION,
            AccountStatus.SIGNED_IN,
        }:
            if self._snapshot is None and not self._busy:
                self.restore()
            return
        if snapshot.status is AccountStatus.EXPIRED:
            self._cancel_for_account_change(
                clear=False,
                clear_runtime_additions=True,
            )
            if self._snapshot is None and not self._busy:
                self.restore()

    @Slot(str, object)
    def _on_account_operation_completed(
        self,
        command: str,
        snapshot: AccountSnapshot,
    ) -> None:
        if self._disposed or not isinstance(snapshot, AccountSnapshot):
            return
        if command in {"restore", "login"} and snapshot.status in {
            AccountStatus.SAVED_SESSION,
            AccountStatus.SIGNED_IN,
        }:
            if self._busy and self._command == "restore":
                return
            if self._busy:
                self._cancel_for_account_change(clear=False)
            self.restore()
        elif command == "logout":
            self._cancel_for_account_change(
                clear=True,
                clear_runtime_additions=True,
            )

    def _cancel_for_account_change(
        self,
        *,
        clear: bool,
        clear_runtime_additions: bool = False,
        clear_known: bool = False,
    ) -> None:
        self.service.cancel_operations()
        self._generation += 1
        self._mailbox.invalidate(self._generation)
        self.progress_changed.emit(None)
        self._set_busy(False, "")
        self._invalidate_filter()
        if clear:
            self.service.clear_memory()
            self._snapshot = None
            self.snapshot_changed.emit(None)
            clear_known = True
        if clear_known:
            self._publish_known_favorite_ids(frozenset())
        elif clear_runtime_additions:
            self._publish_known_favorite_ids(_favorite_ids(self._snapshot))

    def _publish_snapshot(
        self,
        snapshot: FavoritesSnapshot,
        *,
        rebuild_known: bool = True,
    ) -> None:
        self._invalidate_filter()
        self._snapshot = snapshot
        self.snapshot_changed.emit(snapshot)
        if rebuild_known:
            self._publish_known_favorite_ids(_favorite_ids(snapshot))

    def _publish_known_favorite_ids(self, album_ids) -> None:
        album_ids = frozenset(album_ids)
        if album_ids == self._known_favorite_ids:
            return
        self._known_favorite_ids = album_ids
        self.known_favorite_ids_changed.emit(album_ids)

    def _set_busy(self, busy: bool, command: str) -> None:
        busy = bool(busy)
        command = command if busy else ""
        if busy == self._busy and command == self._command:
            return
        self._busy = busy
        self._command = command
        self.busy_changed.emit(busy, command)
        self._set_add_available(self._calculate_add_available())

    def _calculate_add_available(self) -> bool:
        return (
            not self._disposed
            and not self._busy
            and self._account_snapshot.status
            in {AccountStatus.SAVED_SESSION, AccountStatus.SIGNED_IN}
        )

    def _set_add_available(self, available: bool) -> None:
        available = bool(available)
        if available == self._add_available:
            return
        self._add_available = available
        self.add_availability_changed.emit(available)

    def _invalidate_filter(self) -> None:
        self._filter_generation += 1
        self._filter_mailbox.invalidate(self._filter_generation)
        self._filter_snapshot = None
        self._set_filter_busy(False)

    def _set_filter_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy == self._filter_busy:
            return
        self._filter_busy = busy
        self.filter_busy_changed.emit(busy)


def _favorite_ids(snapshot: FavoritesSnapshot | None) -> frozenset[str]:
    if snapshot is None:
        return frozenset()
    return frozenset(
        item.album_id
        for folder in snapshot.folders
        for item in folder.items
    )


def _folder_by_id(
    snapshot: FavoritesSnapshot | None,
    folder_id: str,
) -> FavoriteFolderSnapshot | None:
    if snapshot is None or not isinstance(folder_id, str):
        return None
    for folder in snapshot.folders:
        if folder.folder_id == folder_id:
            return folder
    return None


def _filter_folder_items(
    folder: FavoriteFolderSnapshot,
    keyword: str,
) -> FavoritesFilterSnapshot:
    if not keyword:
        items = folder.items
    else:
        items = tuple(
            item
            for item in folder.items
            if any(
                keyword in _normalize_filter_text(value)
                for value in (
                    item.album_id,
                    f"jm{item.album_id}",
                    item.title or "",
                    *item.authors,
                )
            )
        )
    return FavoritesFilterSnapshot(folder.folder_id, keyword, items)


def _normalize_filter_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _partial_move_error_payload(error: Exception) -> tuple[str, str]:
    code, _message = _safe_error_payload(error)
    if code == FavoritesSessionExpired.code:
        return (
            code,
            "已收藏，但登录会话已过期，未能移动到所选收藏夹",
        )
    if code == FavoritesMutationUncertain.code:
        return (
            code,
            "已收藏，但移动结果无法确认，请同步收藏后核对",
        )
    return (
        "move_failed_after_add",
        "已收藏，但移动失败，当前可能位于默认位置",
    )


def _safe_album_id(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    album_id = value.strip()
    if album_id[:2].lower() == "jm":
        album_id = album_id[2:].strip()
    if (
        not album_id
        or len(album_id) > 32
        or not album_id.isascii()
        or not album_id.isdigit()
    ):
        return None
    return str(int(album_id))


def _snapshot_order(snapshot: FavoritesSnapshot | None) -> str:
    return snapshot.order_by if snapshot is not None else "mr"


def _safe_order_by(
    value: str | None,
    snapshot: FavoritesSnapshot | None,
) -> str | None:
    if value is None:
        return _snapshot_order(snapshot)
    if value not in {"mr", "mp"}:
        return None
    return value


def _safe_existing_folder_id(
    value,
    snapshot: FavoritesSnapshot | None,
    *,
    allow_all: bool,
) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    folder_id = str(int(value))
    if folder_id == "0" and not allow_all:
        return None
    if snapshot is None or snapshot.synced_at_utc is None:
        return None
    if not any(folder.folder_id == folder_id for folder in snapshot.folders):
        return None
    return folder_id


def _safe_target_folder_id(
    value,
    snapshot: FavoritesSnapshot | None,
) -> str | None:
    if value == "0":
        return "0"
    return _safe_existing_folder_id(value, snapshot, allow_all=False)


__all__ = ["FavoritesController", "DEFAULT_RESULT_INTERVAL_MS"]
