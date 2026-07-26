from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import threading

from .account import AccountValidationError, validate_login_credentials
from .protected_store import (
    ProtectedStore,
    ProtectedStoreError,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
)


CREDENTIALS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RememberedCredentials:
    username: str
    password: str = field(repr=False)


class CredentialStore:
    def __init__(self, protected_store: ProtectedStore):
        if not isinstance(protected_store, ProtectedStore):
            raise TypeError("protected_store must be ProtectedStore")
        if protected_store.kind.value != "credentials":
            raise TypeError(
                "protected_store must be bound to credentials data"
            )
        self.protected_store = protected_store
        self._lock = threading.RLock()
        self.last_recovery_backup: Path | None = None

    @property
    def path(self) -> Path:
        return self.protected_store.path

    def load(self) -> RememberedCredentials | None:
        with self._lock:
            self.last_recovery_backup = None
            try:
                payload = self.protected_store.load()
                if payload is None:
                    return None
                return self._decode(payload)
            except (
                ProtectedStoreUnreadableError,
                ProtectedStoreValidationError,
            ):
                self._backup_unreadable()
                return None

    def save(self, username: str, password: str) -> None:
        try:
            username, password = validate_login_credentials(
                username,
                password,
            )
        except AccountValidationError:
            raise ProtectedStoreValidationError("记忆凭据格式无效") from None
        with self._lock:
            self.protected_store.save(
                {
                    "schema_version": CREDENTIALS_SCHEMA_VERSION,
                    "username": username,
                    "password": password,
                }
            )

    def delete(self) -> None:
        with self._lock:
            self.protected_store.delete()

    @staticmethod
    def _decode(payload: dict) -> RememberedCredentials:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "username",
            "password",
        }:
            raise ProtectedStoreValidationError("记忆凭据字段无效")
        if payload.get("schema_version") != CREDENTIALS_SCHEMA_VERSION:
            raise ProtectedStoreValidationError("记忆凭据版本无效")
        try:
            username, password = validate_login_credentials(
                payload.get("username"),
                payload.get("password"),
            )
        except AccountValidationError:
            raise ProtectedStoreValidationError("记忆凭据格式无效") from None
        return RememberedCredentials(username, password)

    def _backup_unreadable(self) -> None:
        source = self.path
        if not source.exists() or source.is_symlink() or not source.is_file():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = source.with_name(f"{source.name}.corrupt-{stamp}")
        suffix = 1
        while backup.exists():
            backup = source.with_name(
                f"{source.name}.corrupt-{stamp}-{suffix}"
            )
            suffix += 1
        try:
            os.replace(source, backup)
        except OSError:
            return
        self.last_recovery_backup = backup


__all__ = [
    "CREDENTIALS_SCHEMA_VERSION",
    "CredentialStore",
    "RememberedCredentials",
]
