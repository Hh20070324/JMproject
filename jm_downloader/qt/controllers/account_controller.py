import logging
import threading
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...account import (
    AccountError,
    AccountLocalDataError,
    AccountOperationCancelled,
    AccountRejected,
    AccountResponseError,
    AccountService,
    AccountStorageError,
    AccountSwitchRequired,
    AccountUnavailable,
    AccountValidationError,
    validate_login_credentials,
)
from ...models import AccountSnapshot, AccountStatus


LOGGER = logging.getLogger("jm-downloader")
DEFAULT_RESULT_INTERVAL_MS = 15


@dataclass(slots=True)
class _AccountJob:
    generation: int
    operation: int
    command: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    def clear_secret(self) -> None:
        self.password = None


@dataclass(frozen=True, slots=True)
class _AccountOutcome:
    generation: int
    command: str
    snapshot: AccountSnapshot | None = None
    error_code: str | None = None
    error_message: str | None = None


class _AccountMailbox:
    def __init__(self, service: AccountService):
        self.service = service
        self.condition = threading.Condition()
        self.pending: _AccountJob | None = None
        self.completed: deque[_AccountOutcome] = deque()
        self.latest_generation = 0
        self.stopped = False

    def submit(self, job: _AccountJob) -> bool:
        with self.condition:
            if self.stopped:
                job.clear_secret()
                return False
            if self.pending is not None:
                self.pending.clear_secret()
            self.latest_generation = job.generation
            self.pending = job
            self.completed.clear()
            self.condition.notify()
            return True

    def next_job(self) -> _AccountJob | None:
        with self.condition:
            while self.pending is None and not self.stopped:
                self.condition.wait()
            if self.pending is not None:
                job = self.pending
                self.pending = None
                return job
            return None

    def publish(self, outcome: _AccountOutcome) -> None:
        with self.condition:
            if self.stopped or outcome.generation != self.latest_generation:
                return
            self.completed.append(outcome)

    def take_completed(self) -> tuple[_AccountOutcome, ...]:
        with self.condition:
            outcomes = tuple(self.completed)
            self.completed.clear()
            return outcomes

    def close(self, *_args) -> None:
        with self.condition:
            if self.stopped:
                return
            self.stopped = True
            self.latest_generation += 1
            if self.pending is not None and self.pending.command != "logout":
                self.pending.clear_secret()
                self.pending = None
            self.completed.clear()
            self.condition.notify_all()


def _account_worker(mailbox: _AccountMailbox) -> None:
    while True:
        job = mailbox.next_job()
        if job is None:
            return
        try:
            if job.command == "restore":
                snapshot = mailbox.service.restore(job.operation)
            elif job.command == "login":
                password = job.password
                job.clear_secret()
                if password is None:
                    raise AccountValidationError()
                try:
                    snapshot = mailbox.service.login(
                        job.username or "",
                        password,
                        job.operation,
                    )
                finally:
                    password = None
            elif job.command == "logout":
                snapshot = mailbox.service.logout(job.operation)
            else:
                raise AccountError()
            outcome = _AccountOutcome(
                job.generation,
                job.command,
                snapshot=snapshot,
            )
        except Exception as error:
            code, message = _safe_error_payload(error)
            if code != AccountOperationCancelled.code:
                LOGGER.warning(
                    "Account worker failed: generation=%s command=%s "
                    "category=%s error_type=%s",
                    job.generation,
                    job.command,
                    code,
                    type(error).__name__,
                )
            outcome = _AccountOutcome(
                job.generation,
                job.command,
                snapshot=mailbox.service.snapshot,
                error_code=code,
                error_message=message,
            )
        finally:
            job.clear_secret()
        mailbox.publish(outcome)


def _safe_error_payload(error: Exception) -> tuple[str, str]:
    if isinstance(error, AccountValidationError):
        return error.code, str(error)
    if isinstance(error, AccountStorageError):
        return error.code, str(error)
    for error_type in (
        AccountRejected,
        AccountUnavailable,
        AccountResponseError,
        AccountLocalDataError,
        AccountSwitchRequired,
        AccountOperationCancelled,
    ):
        if isinstance(error, error_type):
            return error_type.code, error_type.default_message
    return AccountError.code, AccountError.default_message


class AccountController(QObject):
    snapshot_changed = Signal(object)
    operation_failed = Signal(str, str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        service: AccountService,
        parent=None,
        result_interval_ms: int = DEFAULT_RESULT_INTERVAL_MS,
        auto_restore: bool = True,
    ):
        super().__init__(parent)
        if not isinstance(service, AccountService):
            raise TypeError("service must be AccountService")
        if type(result_interval_ms) is not int or result_interval_ms < 1:
            raise ValueError("result_interval_ms must be a positive integer")
        self.service = service
        self._mailbox = _AccountMailbox(service)
        self._generation = 0
        self._snapshot = service.snapshot
        self._busy = False
        self._disposed = False
        self._logout_pending = False

        self._workers = tuple(
            threading.Thread(
                target=_account_worker,
                args=(self._mailbox,),
                name=f"jm-account-{index + 1}",
                daemon=True,
            )
            for index in range(2)
        )
        for worker in self._workers:
            worker.start()

        self._result_timer = QTimer(self)
        self._result_timer.setInterval(result_interval_ms)
        self._result_timer.timeout.connect(self._drain_results)
        self._result_timer.start()
        self.destroyed.connect(self._mailbox.close)
        if auto_restore:
            self.restore()

    @property
    def current_snapshot(self) -> AccountSnapshot:
        return self._snapshot

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def worker_is_daemon(self) -> bool:
        return all(worker.daemon for worker in self._workers)

    @Slot()
    def restore(self) -> int | None:
        if self._disposed or self._busy:
            return None
        operation = self.service.start_operation()
        return self._submit(
            "restore",
            operation,
            AccountSnapshot(AccountStatus.RESTORING),
        )

    @Slot(str, str)
    def login(self, username: str, password: str) -> int | None:
        if self._disposed or self._busy:
            return None
        try:
            username, password = validate_login_credentials(username, password)
        except AccountValidationError as error:
            self.operation_failed.emit(error.code, str(error))
            return None
        operation = self.service.start_operation()
        return self._submit(
            "login",
            operation,
            AccountSnapshot(AccountStatus.SIGNING_IN, username),
            username=username,
            password=password,
        )

    @Slot()
    def logout(self) -> int | None:
        if self._disposed:
            return None
        operation = self.service.prepare_logout()
        self._logout_pending = True
        return self._submit(
            "logout",
            operation,
            AccountSnapshot(AccountStatus.SIGNED_OUT),
            force=True,
        )

    def mark_expired(self) -> None:
        if self._disposed:
            return
        self._publish_snapshot(self.service.mark_expired())

    @Slot()
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if not self._logout_pending:
            self.service.invalidate_operations()
        self._result_timer.stop()
        self._mailbox.close()
        self._busy = False

    def _submit(
        self,
        command: str,
        operation: int,
        transient: AccountSnapshot,
        *,
        username: str | None = None,
        password: str | None = None,
        force: bool = False,
    ) -> int | None:
        if self._disposed or (self._busy and not force):
            return None
        self._generation += 1
        job = _AccountJob(
            self._generation,
            operation,
            command,
            username,
            password,
        )
        if not self._mailbox.submit(job):
            return None
        self._publish_snapshot(transient)
        self._set_busy(True)
        return job.generation

    @Slot()
    def _drain_results(self) -> None:
        if self._disposed:
            return
        for outcome in self._mailbox.take_completed():
            if self._disposed or outcome.generation != self._generation:
                continue
            self._set_busy(False)
            if outcome.command == "logout":
                self._logout_pending = False
            if outcome.snapshot is not None:
                self._publish_snapshot(outcome.snapshot)
            if outcome.error_code == AccountOperationCancelled.code:
                continue
            if outcome.error_code is not None:
                self.operation_failed.emit(
                    outcome.error_code,
                    outcome.error_message or AccountError.default_message,
                )

    def _publish_snapshot(self, snapshot: AccountSnapshot) -> None:
        self._snapshot = snapshot
        self.snapshot_changed.emit(snapshot)

    def _set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        self.busy_changed.emit(busy)


__all__ = ["AccountController", "DEFAULT_RESULT_INTERVAL_MS"]
