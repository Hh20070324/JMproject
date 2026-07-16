from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading

import jmcomic
from curl_cffi.requests.exceptions import RequestException

from .jmcomic_client import serialized_client_construction
from .models import AccountSnapshot, AccountStatus
from .protected_store import (
    ProtectedStore,
    ProtectedStoreDeleteError,
    ProtectedStoreError,
    ProtectedStorePathError,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
    ProtectedStoreWriteError,
    UnsupportedProtectedPayloadVersion,
    UnsupportedProtectedStoreVersion,
)
from .settings import AppPaths, DEFAULT_PATHS


LOGGER = logging.getLogger("jm-downloader")
ACCOUNT_TIMEOUT_SECONDS = 15
ACCOUNT_REQUEST_RETRIES = 1
ACCOUNT_PAYLOAD_SCHEMA_VERSION = 1
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 512
MAX_COOKIE_VALUE_LENGTH = 4096
MAX_COOKIE_TOTAL_LENGTH = 16 * 1024
ALLOWED_SESSION_COOKIE_NAMES = frozenset(
    {
        "AVS",
        "PHPSESSID",
        "igneous",
        "ipb_member_id",
        "ipb_pass_hash",
        "session",
    }
)


class AccountError(Exception):
    code = "unknown"
    default_message = "账号操作暂时失败，请稍后重试"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


class AccountValidationError(AccountError):
    code = "validation"
    default_message = "账号或密码格式无效"


class AccountRejected(AccountError):
    code = "rejected"
    default_message = "账号或密码错误"


class AccountUnavailable(AccountError):
    code = "unavailable"
    default_message = "网络暂不可用，请稍后重试"


class AccountResponseError(AccountError):
    code = "invalid_response"
    default_message = "登录服务响应异常"


class AccountStorageError(AccountError):
    code = "storage"
    default_message = "无法保存本地登录信息"


class AccountLocalDataError(AccountError):
    code = "local_data_unreadable"
    default_message = "本地登录信息无法读取"


class AccountSwitchRequired(AccountError):
    code = "switch_requires_logout"
    default_message = "请先退出当前账号，再登录其他账号"


class AccountOperationCancelled(AccountError):
    code = "cancelled"
    default_message = "账号操作已取消"


@dataclass(frozen=True, slots=True)
class AccountSession:
    uid: str
    username: str
    cookies: tuple[tuple[str, str], ...]
    last_verified_at_utc: str

    def cookie_dict(self) -> dict[str, str]:
        return dict(self.cookies)

    def to_payload(self) -> dict:
        return {
            "schema_version": ACCOUNT_PAYLOAD_SCHEMA_VERSION,
            "uid": self.uid,
            "username": self.username,
            "cookies": self.cookie_dict(),
            "last_verified_at_utc": self.last_verified_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: Mapping) -> "AccountSession":
        if not isinstance(payload, Mapping):
            raise AccountLocalDataError()
        version = payload.get("schema_version")
        if type(version) is not int or version != ACCOUNT_PAYLOAD_SCHEMA_VERSION:
            raise AccountLocalDataError()
        if set(payload) != {
            "schema_version",
            "uid",
            "username",
            "cookies",
            "last_verified_at_utc",
        }:
            raise AccountLocalDataError()
        try:
            uid = _validate_uid(payload.get("uid"))
            username = _validate_display_username(payload.get("username"))
            cookies = _validate_cookie_mapping(payload.get("cookies"))
            verified_at = _validate_utc_timestamp(
                payload.get("last_verified_at_utc")
            )
        except AccountValidationError:
            raise AccountLocalDataError() from None
        return cls(uid, username, cookies, verified_at)


class AccountStore:
    def __init__(self, protected: ProtectedStore):
        if not isinstance(protected, ProtectedStore):
            raise TypeError("protected must be ProtectedStore")
        if protected.kind.value != "account":
            raise TypeError("protected store must be bound to account data")
        self.protected = protected

    @classmethod
    def create(cls, paths: AppPaths = DEFAULT_PATHS) -> "AccountStore":
        return cls(ProtectedStore.account(paths))

    def load(self) -> AccountSession | None:
        try:
            payload = self.protected.load()
        except (
            ProtectedStorePathError,
            ProtectedStoreUnreadableError,
            ProtectedStoreValidationError,
            UnsupportedProtectedPayloadVersion,
            UnsupportedProtectedStoreVersion,
        ):
            raise AccountLocalDataError() from None
        if payload is None:
            return None
        return AccountSession.from_payload(payload)

    def save(self, session: AccountSession) -> None:
        if not isinstance(session, AccountSession):
            raise TypeError("session must be AccountSession")
        try:
            self.protected.save(session.to_payload())
        except (ProtectedStoreError, OSError):
            raise AccountStorageError() from None

    def delete(self) -> None:
        try:
            self.protected.delete()
        except (ProtectedStoreDeleteError, ProtectedStorePathError, OSError):
            raise AccountStorageError("无法删除 account.dat") from None


def build_account_client(
    option_file: Path,
    cookies: Mapping[str, str] | None = None,
):
    with serialized_client_construction():
        option = jmcomic.create_option_by_file(str(option_file))
        option.client.retry_times = 0
        arguments = {
            "impl": "api",
            "timeout": ACCOUNT_TIMEOUT_SECONDS,
        }
        if cookies:
            arguments["cookies"] = dict(cookies)
        client = option.new_jm_client(**arguments)
        if not isinstance(client, jmcomic.JmApiClient):
            raise TypeError("unexpected client type")
        if client.get_meta_data("timeout") != ACCOUNT_TIMEOUT_SECONDS:
            raise ValueError("timeout was not applied")
        client.retry_times = ACCOUNT_REQUEST_RETRIES
        return client


class AccountService:
    def __init__(
        self,
        paths: AppPaths = DEFAULT_PATHS,
        account_store: AccountStore | None = None,
        favorites_store: ProtectedStore | None = None,
        client_factory: Callable[[Mapping[str, str] | None], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(paths, AppPaths):
            raise TypeError("paths must be AppPaths")
        if client_factory is not None and not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.paths = paths
        self.account_store = account_store or AccountStore.create(paths)
        self.favorites_store = favorites_store or ProtectedStore.favorites(paths)
        self._client_factory = client_factory or (
            lambda cookies=None: build_account_client(
                self.paths.option_file,
                cookies,
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._protected_data_lock = threading.RLock()
        self._operation_generation = 0
        self._discard_stale_session = False
        self._session: AccountSession | None = None
        self._snapshot = AccountSnapshot(AccountStatus.SIGNED_OUT)

    @property
    def snapshot(self) -> AccountSnapshot:
        with self._lock:
            return self._snapshot

    def start_operation(self) -> int:
        with self._lock:
            self._operation_generation += 1
            self._discard_stale_session = False
            return self._operation_generation

    def invalidate_operations(self) -> None:
        with self._lock:
            self._operation_generation += 1

    def prepare_logout(self) -> int:
        with self._lock:
            self._operation_generation += 1
            self._discard_stale_session = True
            self._session = None
            self._snapshot = AccountSnapshot(AccountStatus.SIGNED_OUT)
            return self._operation_generation

    def restore(self, operation: int) -> AccountSnapshot:
        try:
            session = self.account_store.load()
        except AccountLocalDataError:
            snapshot = AccountSnapshot(AccountStatus.LOCAL_DATA_UNREADABLE)
            return self._publish_if_current(operation, None, snapshot)
        self._ensure_current(operation)
        if session is None:
            snapshot = AccountSnapshot(AccountStatus.SIGNED_OUT)
        else:
            snapshot = _session_snapshot(session, AccountStatus.SAVED_SESSION)
        return self._publish_if_current(operation, session, snapshot)

    def login(
        self,
        username: str,
        password: str,
        operation: int,
    ) -> AccountSnapshot:
        username, password = validate_login_credentials(username, password)
        with self._lock:
            old_session = self._session
            old_status = self._snapshot.status
        if old_session is not None and old_status is not AccountStatus.EXPIRED:
            raise AccountSwitchRequired()
        self._ensure_current(operation)

        try:
            client = self._client_factory(None)
            response = client.login(username, password)
        except AccountError:
            raise
        except Exception as error:
            mapped = _map_login_error(error)
            LOGGER.warning(
                "Account login failed: category=%s error_type=%s",
                mapped.code,
                type(error).__name__,
            )
            raise mapped from None
        finally:
            password = ""

        self._ensure_current(operation)
        session = self._adapt_login(client, response)
        with self._protected_data_lock:
            self.account_store.save(session)
            if not self._is_current(operation):
                self._rollback_account(self._rollback_session(old_session))
                raise AccountOperationCancelled()

            if old_session is not None and old_session.uid != session.uid:
                try:
                    self.favorites_store.delete()
                except ProtectedStoreError:
                    self._rollback_account(self._rollback_session(old_session))
                    raise AccountStorageError(
                        "无法清除原账号的 favorites.dat"
                    ) from None
            if not self._is_current(operation):
                self._rollback_account(self._rollback_session(old_session))
                raise AccountOperationCancelled()

        snapshot = _session_snapshot(session, AccountStatus.SIGNED_IN)
        return self._publish_if_current(operation, session, snapshot)

    def logout(self, operation: int) -> AccountSnapshot:
        self._ensure_current(operation)
        failures = []
        with self._protected_data_lock:
            try:
                self.account_store.delete()
            except AccountStorageError:
                failures.append("account.dat")
            try:
                self.favorites_store.delete()
            except ProtectedStoreError:
                failures.append("favorites.dat")
        self._ensure_current(operation)
        if failures:
            snapshot = AccountSnapshot(AccountStatus.LOCAL_DATA_UNREADABLE)
            with self._lock:
                if operation == self._operation_generation:
                    self._session = None
                    self._snapshot = snapshot
            raise AccountStorageError(
                "无法删除本地文件：" + "、".join(failures)
            )
        return self._publish_if_current(
            operation,
            None,
            AccountSnapshot(AccountStatus.SIGNED_OUT),
        )

    def mark_expired(self) -> AccountSnapshot:
        with self._lock:
            if self._session is None:
                self._snapshot = AccountSnapshot(AccountStatus.SIGNED_OUT)
            else:
                self._snapshot = _session_snapshot(
                    self._session,
                    AccountStatus.EXPIRED,
                )
            return self._snapshot

    def confirm_session(self, expected: AccountSession) -> AccountSnapshot:
        if not isinstance(expected, AccountSession):
            raise TypeError("expected must be AccountSession")
        with self._lock:
            if self._session != expected or self._snapshot.status not in {
                AccountStatus.SAVED_SESSION,
                AccountStatus.SIGNED_IN,
            }:
                raise AccountOperationCancelled()
            self._snapshot = _session_snapshot(
                self._session,
                AccountStatus.SIGNED_IN,
            )
            return self._snapshot

    def expire_session(self, expected: AccountSession) -> AccountSnapshot:
        if not isinstance(expected, AccountSession):
            raise TypeError("expected must be AccountSession")
        with self._lock:
            if self._session != expected:
                raise AccountOperationCancelled()
            self._snapshot = _session_snapshot(
                self._session,
                AccountStatus.EXPIRED,
            )
            return self._snapshot

    def current_session(self) -> AccountSession:
        with self._lock:
            if self._session is None or self._snapshot.status not in {
                AccountStatus.SAVED_SESSION,
                AccountStatus.SIGNED_IN,
            }:
                raise AccountLocalDataError()
            return self._session

    def local_session(self) -> AccountSession:
        with self._lock:
            if self._session is None or self._snapshot.status not in {
                AccountStatus.SAVED_SESSION,
                AccountStatus.SIGNED_IN,
                AccountStatus.EXPIRED,
            }:
                raise AccountLocalDataError()
            return self._session

    @contextmanager
    def protected_session_data(self, expected: AccountSession):
        if not isinstance(expected, AccountSession):
            raise TypeError("expected must be AccountSession")
        with self._protected_data_lock:
            with self._lock:
                if self._session != expected or self._snapshot.status not in {
                    AccountStatus.SAVED_SESSION,
                    AccountStatus.SIGNED_IN,
                }:
                    raise AccountOperationCancelled()
            yield

    def _adapt_login(self, client, response) -> AccountSession:
        data = getattr(response, "res_data", None)
        getter = getattr(data, "get", None)
        if not callable(getter):
            raise AccountResponseError()
        try:
            uid = _validate_uid(getter("uid"))
            username = _validate_display_username(getter("username"))
            raw_cookies = client.get_meta_data("cookies")
            cookies = _filter_login_cookies(raw_cookies)
            verified_at = _format_utc(self._clock())
        except AccountValidationError:
            raise AccountResponseError() from None
        except Exception:
            raise AccountResponseError() from None
        return AccountSession(uid, username, cookies, verified_at)

    def _rollback_account(self, old_session: AccountSession | None) -> None:
        try:
            if old_session is None:
                self.account_store.delete()
            else:
                self.account_store.save(old_session)
        except AccountError:
            with self._lock:
                self._session = None
                self._snapshot = AccountSnapshot(
                    AccountStatus.LOCAL_DATA_UNREADABLE
                )
            raise AccountStorageError(
                "账号操作取消后无法恢复原登录信息"
            ) from None

    def _rollback_session(
        self,
        old_session: AccountSession | None,
    ) -> AccountSession | None:
        with self._lock:
            if self._discard_stale_session:
                return None
            return old_session

    def _publish_if_current(
        self,
        operation: int,
        session: AccountSession | None,
        snapshot: AccountSnapshot,
    ) -> AccountSnapshot:
        with self._lock:
            if operation != self._operation_generation:
                raise AccountOperationCancelled()
            self._session = session
            self._snapshot = snapshot
            return snapshot

    def _ensure_current(self, operation: int) -> None:
        if not self._is_current(operation):
            raise AccountOperationCancelled()

    def _is_current(self, operation: int) -> bool:
        with self._lock:
            return operation == self._operation_generation


def validate_login_credentials(username: str, password: str) -> tuple[str, str]:
    if not isinstance(username, str) or not isinstance(password, str):
        raise AccountValidationError()
    username = username.strip()
    if (
        not username
        or len(username) > MAX_USERNAME_LENGTH
        or _contains_control(username)
    ):
        raise AccountValidationError("账号格式无效")
    if (
        not password
        or len(password) > MAX_PASSWORD_LENGTH
        or "\0" in password
        or "\r" in password
        or "\n" in password
    ):
        raise AccountValidationError("密码格式无效")
    return username, password


def _validate_uid(value) -> str:
    if type(value) is int:
        value = str(value)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32
        or not value.isascii()
        or not value.isdigit()
    ):
        raise AccountValidationError()
    return value


def _validate_display_username(value) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_USERNAME_LENGTH
        or _contains_control(value)
    ):
        raise AccountValidationError()
    return value.strip()


def _filter_login_cookies(value) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise AccountValidationError()
    filtered = {
        key: cookie
        for key, cookie in value.items()
        if key in ALLOWED_SESSION_COOKIE_NAMES
    }
    if "AVS" not in filtered:
        raise AccountValidationError()
    return _validate_cookie_mapping(filtered)


def _validate_cookie_mapping(value) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not value:
        raise AccountValidationError()
    cookies = []
    total = 0
    for key, cookie in value.items():
        if key not in ALLOWED_SESSION_COOKIE_NAMES:
            raise AccountValidationError()
        if (
            not isinstance(cookie, str)
            or not cookie
            or len(cookie) > MAX_COOKIE_VALUE_LENGTH
            or not cookie.isascii()
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in cookie)
        ):
            raise AccountValidationError()
        total += len(key) + len(cookie)
        cookies.append((key, cookie))
    if "AVS" not in dict(cookies) or total > MAX_COOKIE_TOTAL_LENGTH:
        raise AccountValidationError()
    cookies.sort()
    return tuple(cookies)


def _validate_utc_timestamp(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise AccountValidationError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AccountValidationError() from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AccountValidationError()
    return _format_utc(parsed)


def _format_utc(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise AccountValidationError()
    if value.tzinfo is None:
        raise AccountValidationError()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_control(value: str) -> bool:
    return "\0" in value or any(ord(character) < 0x20 for character in value)


def _session_snapshot(
    session: AccountSession,
    status: AccountStatus,
) -> AccountSnapshot:
    return AccountSnapshot(
        status,
        session.username,
        session.last_verified_at_utc,
    )


def _map_login_error(error: Exception) -> AccountError:
    if isinstance(error, AccountError):
        return error
    if isinstance(
        error,
        (
            jmcomic.RequestRetryAllFailException,
            RequestException,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return AccountUnavailable()
    if isinstance(error, (KeyError, PermissionError)):
        return AccountRejected()
    if isinstance(error, jmcomic.ResponseUnexpectedException):
        status = _response_status(error)
        if status is not None and 400 <= status < 500:
            return AccountRejected()
        return AccountResponseError()
    if isinstance(
        error,
        (
            jmcomic.RegularNotMatchException,
            jmcomic.JsonResolveFailException,
            jmcomic.JmcomicException,
        ),
    ):
        return AccountResponseError()
    return AccountError()


def _response_status(error: Exception) -> int | None:
    try:
        response = error.resp
    except Exception:
        return None
    for attribute in ("http_code", "status_code"):
        try:
            value = getattr(response, attribute)
        except Exception:
            continue
        if type(value) is int:
            return value
    return None


__all__ = [
    "ACCOUNT_REQUEST_RETRIES",
    "ACCOUNT_TIMEOUT_SECONDS",
    "AccountError",
    "AccountLocalDataError",
    "AccountOperationCancelled",
    "AccountRejected",
    "AccountResponseError",
    "AccountService",
    "AccountSession",
    "AccountStorageError",
    "AccountStore",
    "AccountSwitchRequired",
    "AccountUnavailable",
    "AccountValidationError",
    "build_account_client",
    "validate_login_credentials",
]
