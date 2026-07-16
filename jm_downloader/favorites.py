from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading

import jmcomic
from curl_cffi.requests.exceptions import RequestException

from .account import (
    AccountLocalDataError,
    AccountOperationCancelled,
    AccountService,
    AccountSession,
    build_account_client,
)
from .models import (
    FavoriteFolderSnapshot,
    FavoriteItemSnapshot,
    FavoritesSnapshot,
    FavoritesSyncProgress,
)
from .protected_store import (
    ProtectedStore,
    ProtectedStoreError,
    ProtectedStorePathError,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
    UnsupportedProtectedPayloadVersion,
    UnsupportedProtectedStoreVersion,
)
from .settings import AppPaths, DEFAULT_PATHS
from .tasks import InvalidAlbumId, normalize_album_id


LOGGER = logging.getLogger("jm-downloader")
FAVORITES_PAYLOAD_SCHEMA_VERSION = 1
DEFAULT_FOLDER_ID = "0"
DEFAULT_FOLDER_NAME = "默认收藏"
MAX_FOLDER_COUNT = 1_000
MAX_FOLDER_ID_LENGTH = 32
MAX_FOLDER_NAME_LENGTH = 256
MAX_TOTAL_ITEMS = 100_000
MAX_PAGES_PER_FOLDER = 10_000
MAX_ITEMS_PER_PAGE = 1_000
MAX_TITLE_LENGTH = 4_096
MAX_METADATA_VALUE_LENGTH = 512
MAX_METADATA_VALUES = 64


class FavoritesError(Exception):
    code = "unknown"
    default_message = "收藏操作暂时失败，请稍后重试"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


class FavoritesSessionRequired(FavoritesError):
    code = "requires_login"
    default_message = "请先登录账号"


class FavoritesSessionExpired(FavoritesError):
    code = "session_expired"
    default_message = "登录会话已过期，请重新登录"


class FavoritesUnavailable(FavoritesError):
    code = "unavailable"
    default_message = "网络暂不可用，已保留上次同步内容"


class FavoritesResponseError(FavoritesError):
    code = "invalid_response"
    default_message = "收藏服务响应异常，已保留上次同步内容"


class FavoritesLocalDataError(FavoritesError):
    code = "local_data_unreadable"
    default_message = "本地收藏数据无法读取"


class FavoritesAccountMismatch(FavoritesLocalDataError):
    code = "account_mismatch"
    default_message = "本地收藏属于其他账号，无法显示"


class FavoritesStorageError(FavoritesError):
    code = "storage"
    default_message = "无法保存本地收藏数据"


class FavoritesOperationCancelled(FavoritesError):
    code = "cancelled"
    default_message = "收藏同步已停止"


@dataclass(frozen=True, slots=True)
class FavoritesCache:
    account_uid: str
    synced_at_utc: str
    folders: tuple[FavoriteFolderSnapshot, ...]

    def to_snapshot(self) -> FavoritesSnapshot:
        return FavoritesSnapshot(self.synced_at_utc, self.folders)

    def to_payload(self) -> dict:
        return {
            "schema_version": FAVORITES_PAYLOAD_SCHEMA_VERSION,
            "account_uid": self.account_uid,
            "synced_at_utc": self.synced_at_utc,
            "folders": [
                {
                    "folder_id": folder.folder_id,
                    "name": folder.name,
                    "items": [
                        {
                            "album_id": item.album_id,
                            "title": item.title,
                            "authors": list(item.authors),
                            "tags": list(item.tags),
                        }
                        for item in folder.items
                    ],
                }
                for folder in self.folders
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping) -> "FavoritesCache":
        try:
            return _cache_from_payload(payload)
        except FavoritesLocalDataError:
            raise
        except Exception:
            raise FavoritesLocalDataError() from None


class FavoriteCacheStore:
    def __init__(self, protected: ProtectedStore):
        if not isinstance(protected, ProtectedStore):
            raise TypeError("protected must be ProtectedStore")
        if protected.kind.value != "favorites":
            raise TypeError("protected store must be bound to favorites data")
        self.protected = protected

    @classmethod
    def create(cls, paths: AppPaths = DEFAULT_PATHS) -> "FavoriteCacheStore":
        return cls(ProtectedStore.favorites(paths))

    def load(self, expected_uid: str) -> FavoritesCache | None:
        expected_uid = _cache_uid(expected_uid)
        try:
            payload = self.protected.load()
        except (
            ProtectedStorePathError,
            ProtectedStoreUnreadableError,
            ProtectedStoreValidationError,
            UnsupportedProtectedPayloadVersion,
            UnsupportedProtectedStoreVersion,
        ):
            raise FavoritesLocalDataError() from None
        if payload is None:
            return None
        cache = FavoritesCache.from_payload(payload)
        if cache.account_uid != expected_uid:
            raise FavoritesAccountMismatch()
        return cache

    def save(self, cache: FavoritesCache) -> None:
        if not isinstance(cache, FavoritesCache):
            raise TypeError("cache must be FavoritesCache")
        try:
            payload = cache.to_payload()
            if FavoritesCache.from_payload(payload) != cache:
                raise FavoritesLocalDataError()
            self.protected.save(payload)
        except FavoritesLocalDataError:
            raise FavoritesStorageError() from None
        except (ProtectedStoreError, OSError):
            raise FavoritesStorageError() from None

    def delete(self) -> None:
        try:
            self.protected.delete()
        except (ProtectedStoreError, OSError):
            raise FavoritesStorageError("无法删除 favorites.dat") from None


class FavoritesService:
    def __init__(
        self,
        account_service: AccountService,
        paths: AppPaths = DEFAULT_PATHS,
        cache_store: FavoriteCacheStore | None = None,
        client_factory: Callable[[Mapping[str, str]], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(account_service, AccountService):
            raise TypeError("account_service must be AccountService")
        if not isinstance(paths, AppPaths):
            raise TypeError("paths must be AppPaths")
        if client_factory is not None and not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.account_service = account_service
        self.paths = paths
        self.cache_store = cache_store or FavoriteCacheStore.create(paths)
        self._client_factory = client_factory or (
            lambda cookies: build_account_client(
                self.paths.option_file,
                cookies,
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._operation_generation = 0
        self._snapshot: FavoritesSnapshot | None = None

    @property
    def snapshot(self) -> FavoritesSnapshot | None:
        with self._lock:
            return self._snapshot

    def start_operation(self) -> int:
        with self._lock:
            self._operation_generation += 1
            return self._operation_generation

    def cancel_operations(self) -> None:
        with self._lock:
            self._operation_generation += 1

    def clear_memory(self) -> None:
        with self._lock:
            self._operation_generation += 1
            self._snapshot = None

    def restore(self, operation: int) -> FavoritesSnapshot:
        session = self._require_local_session()
        cache = self.cache_store.load(session.uid)
        snapshot = (
            FavoritesSnapshot(None, ())
            if cache is None
            else cache.to_snapshot()
        )
        self._ensure_current_local_session(operation, session)
        return self._publish(operation, session, snapshot, allow_expired=True)

    def sync(
        self,
        operation: int,
        progress_callback: Callable[[FavoritesSyncProgress], None] | None = None,
    ) -> FavoritesSnapshot:
        if progress_callback is not None and not callable(progress_callback):
            raise TypeError("progress_callback must be callable")
        session = self._require_session()
        old_cache = self.cache_store.load(session.uid)
        self._ensure_current_session(operation, session)

        try:
            client = self._client_factory(session.cookie_dict())
            folders = self._fetch_all(
                client,
                session,
                operation,
                progress_callback,
            )
        except FavoritesError as error:
            self._handle_sync_error(error, session)
            raise
        except Exception as error:
            mapped = _map_sync_error(error)
            LOGGER.warning(
                "Favorites sync failed: category=%s error_type=%s",
                mapped.code,
                type(error).__name__,
            )
            self._handle_sync_error(mapped, session)
            raise mapped from None

        cache = FavoritesCache(
            session.uid,
            _format_utc(self._clock()),
            folders,
        )
        self._ensure_current_session(operation, session)
        try:
            with self.account_service.protected_session_data(session):
                self._ensure_current_session(operation, session)
                self.cache_store.save(cache)
                try:
                    self._ensure_current_session(operation, session)
                    self.account_service.confirm_session(session)
                    self._ensure_current_session(operation, session)
                    return self._publish(
                        operation,
                        session,
                        cache.to_snapshot(),
                    )
                except (
                    FavoritesOperationCancelled,
                    AccountOperationCancelled,
                ):
                    self._rollback_cache(old_cache, session)
                    raise FavoritesOperationCancelled() from None
        except (FavoritesOperationCancelled, AccountOperationCancelled):
            raise FavoritesOperationCancelled() from None

    def _fetch_all(
        self,
        client,
        session: AccountSession,
        operation: int,
        progress_callback: Callable[[FavoritesSyncProgress], None] | None,
    ) -> tuple[FavoriteFolderSnapshot, ...]:
        self._ensure_current_session(operation, session)
        first_page = client.favorite_folder(
            page=1,
            order_by="mr",
            folder_id=DEFAULT_FOLDER_ID,
            username="",
        )
        folder_specs = _folder_specs(first_page)
        folder_count = len(folder_specs)
        folders = []
        total_items = 0
        for index, (folder_id, folder_name) in enumerate(folder_specs, start=1):
            self._ensure_current_session(operation, session)
            folder = self._fetch_folder(
                client,
                session,
                operation,
                folder_id,
                folder_name,
                index,
                folder_count,
                first_page if folder_id == DEFAULT_FOLDER_ID else None,
                progress_callback,
            )
            total_items += len(folder.items)
            if total_items > MAX_TOTAL_ITEMS:
                raise FavoritesResponseError("收藏数量超过安全上限")
            folders.append(folder)
        return tuple(folders)

    def _fetch_folder(
        self,
        client,
        session: AccountSession,
        operation: int,
        folder_id: str,
        folder_name: str,
        folder_index: int,
        folder_count: int,
        first_page,
        progress_callback: Callable[[FavoritesSyncProgress], None] | None,
    ) -> FavoriteFolderSnapshot:
        requested_page = 1
        response = first_page
        expected_total = None
        expected_page_count = None
        items = []
        seen_ids = set()
        while True:
            self._ensure_current_session(operation, session)
            if response is None:
                response = client.favorite_folder(
                    page=requested_page,
                    order_by="mr",
                    folder_id=folder_id,
                    username="",
                )
            page_items, total, page_count = _adapt_page(response)
            if expected_total is None:
                expected_total = total
                expected_page_count = page_count
            elif total != expected_total or page_count != expected_page_count:
                raise FavoritesResponseError()
            if page_count and requested_page > page_count:
                raise FavoritesResponseError()
            for item in page_items:
                if item.album_id in seen_ids:
                    raise FavoritesResponseError()
                seen_ids.add(item.album_id)
                items.append(item)
            if len(items) > total:
                raise FavoritesResponseError()
            self._emit_progress(
                progress_callback,
                FavoritesSyncProgress(
                    folder_index,
                    folder_count,
                    folder_name,
                    requested_page,
                    page_count,
                    len(items),
                    total,
                ),
            )
            if page_count == 0 or requested_page >= page_count:
                break
            requested_page += 1
            response = None
        if expected_total is None or len(items) != expected_total:
            raise FavoritesResponseError()
        return FavoriteFolderSnapshot(folder_id, folder_name, tuple(items))

    @staticmethod
    def _emit_progress(callback, progress: FavoritesSyncProgress) -> None:
        if callback is None:
            return
        try:
            callback(progress)
        except Exception as error:
            LOGGER.warning(
                "Favorites progress callback failed: error_type=%s",
                type(error).__name__,
            )

    def _handle_sync_error(
        self,
        error: FavoritesError,
        session: AccountSession,
    ) -> None:
        if not isinstance(error, FavoritesSessionExpired):
            return
        try:
            self.account_service.expire_session(session)
        except AccountOperationCancelled:
            raise FavoritesOperationCancelled() from None

    def _rollback_cache(
        self,
        old_cache: FavoritesCache | None,
        expected_session: AccountSession,
    ) -> None:
        try:
            try:
                session_is_current = (
                    self.account_service.local_session() == expected_session
                )
            except AccountLocalDataError:
                session_is_current = False
            if old_cache is None or not session_is_current:
                self.cache_store.delete()
            else:
                self.cache_store.save(old_cache)
        except FavoritesError:
            with self._lock:
                self._snapshot = None
            raise FavoritesStorageError(
                "同步取消后无法恢复原收藏数据"
            ) from None

    def _require_session(self) -> AccountSession:
        try:
            return self.account_service.current_session()
        except AccountLocalDataError:
            raise FavoritesSessionRequired() from None

    def _require_local_session(self) -> AccountSession:
        try:
            return self.account_service.local_session()
        except AccountLocalDataError:
            raise FavoritesSessionRequired() from None

    def _ensure_current_session(
        self,
        operation: int,
        expected: AccountSession,
    ) -> None:
        with self._lock:
            if operation != self._operation_generation:
                raise FavoritesOperationCancelled()
        try:
            current = self.account_service.current_session()
        except AccountLocalDataError:
            raise FavoritesOperationCancelled() from None
        if current != expected:
            raise FavoritesOperationCancelled()

    def _ensure_current_local_session(
        self,
        operation: int,
        expected: AccountSession,
    ) -> None:
        with self._lock:
            if operation != self._operation_generation:
                raise FavoritesOperationCancelled()
        try:
            current = self.account_service.local_session()
        except AccountLocalDataError:
            raise FavoritesOperationCancelled() from None
        if current != expected:
            raise FavoritesOperationCancelled()

    def _publish(
        self,
        operation: int,
        session: AccountSession,
        snapshot: FavoritesSnapshot,
        *,
        allow_expired: bool = False,
    ) -> FavoritesSnapshot:
        if allow_expired:
            self._ensure_current_local_session(operation, session)
        else:
            self._ensure_current_session(operation, session)
        with self._lock:
            if operation != self._operation_generation:
                raise FavoritesOperationCancelled()
            self._snapshot = snapshot
            return snapshot


def _folder_specs(first_page) -> tuple[tuple[str, str], ...]:
    raw_folders = getattr(first_page, "folder_list", None)
    if not isinstance(raw_folders, (list, tuple)):
        raise FavoritesResponseError()
    specs = [(DEFAULT_FOLDER_ID, DEFAULT_FOLDER_NAME)]
    positions = {DEFAULT_FOLDER_ID: 0}
    for raw in raw_folders:
        if not isinstance(raw, Mapping):
            raise FavoritesResponseError()
        folder_id = _remote_folder_id(raw.get("FID"))
        folder_name = _remote_text(
            raw.get("name"),
            MAX_FOLDER_NAME_LENGTH,
            required=True,
        )
        if folder_id in positions:
            if folder_id == DEFAULT_FOLDER_ID and positions[folder_id] == 0:
                specs[0] = (folder_id, folder_name)
                positions[folder_id] = -1
                continue
            raise FavoritesResponseError()
        positions[folder_id] = len(specs)
        specs.append((folder_id, folder_name))
        if len(specs) > MAX_FOLDER_COUNT:
            raise FavoritesResponseError("收藏文件夹数量超过安全上限")
    return tuple(specs)


def _adapt_page(response) -> tuple[tuple[FavoriteItemSnapshot, ...], int, int]:
    raw_content = getattr(response, "content", None)
    if not isinstance(raw_content, (list, tuple)):
        raise FavoritesResponseError()
    if len(raw_content) > MAX_ITEMS_PER_PAGE:
        raise FavoritesResponseError()
    total = _remote_nonnegative_int(getattr(response, "total", None))
    page_count = _remote_nonnegative_int(
        getattr(response, "page_count", None)
    )
    if total > MAX_TOTAL_ITEMS or page_count > MAX_PAGES_PER_FOLDER:
        raise FavoritesResponseError("收藏数量超过安全上限")
    if (total == 0) != (page_count == 0):
        raise FavoritesResponseError()
    if len(raw_content) > total:
        raise FavoritesResponseError()
    items = tuple(_adapt_remote_item(raw) for raw in raw_content)
    return items, total, page_count


def _adapt_remote_item(raw) -> FavoriteItemSnapshot:
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        raise FavoritesResponseError()
    raw_id, raw_info = raw
    album_id = _remote_album_id(raw_id)
    if not isinstance(raw_info, Mapping):
        raise FavoritesResponseError()
    title = _remote_optional_text(
        raw_info.get("name", raw_info.get("title")),
        MAX_TITLE_LENGTH,
    )
    author_value = raw_info.get("author")
    if author_value is None:
        author_value = raw_info.get("authors")
    authors = _remote_text_tuple(author_value)
    tags = _remote_text_tuple(raw_info.get("tags"))
    return FavoriteItemSnapshot(album_id, title, authors, tags)


def _cache_from_payload(payload: Mapping) -> FavoritesCache:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "account_uid",
        "synced_at_utc",
        "folders",
    }:
        raise FavoritesLocalDataError()
    if payload.get("schema_version") != FAVORITES_PAYLOAD_SCHEMA_VERSION:
        raise FavoritesLocalDataError()
    uid = _cache_uid(payload.get("account_uid"))
    synced_at = _cache_timestamp(payload.get("synced_at_utc"))
    raw_folders = payload.get("folders")
    if (
        not isinstance(raw_folders, list)
        or not raw_folders
        or len(raw_folders) > MAX_FOLDER_COUNT
    ):
        raise FavoritesLocalDataError()
    folders = []
    seen_folders = set()
    total_items = 0
    for raw_folder in raw_folders:
        if not isinstance(raw_folder, Mapping) or set(raw_folder) != {
            "folder_id",
            "name",
            "items",
        }:
            raise FavoritesLocalDataError()
        folder_id = _cache_folder_id(raw_folder.get("folder_id"))
        if folder_id in seen_folders:
            raise FavoritesLocalDataError()
        seen_folders.add(folder_id)
        name = _cache_text(
            raw_folder.get("name"),
            MAX_FOLDER_NAME_LENGTH,
            required=True,
        )
        raw_items = raw_folder.get("items")
        if not isinstance(raw_items, list):
            raise FavoritesLocalDataError()
        items = []
        seen_items = set()
        for raw_item in raw_items:
            item = _cache_item(raw_item)
            if item.album_id in seen_items:
                raise FavoritesLocalDataError()
            seen_items.add(item.album_id)
            items.append(item)
        total_items += len(items)
        if total_items > MAX_TOTAL_ITEMS:
            raise FavoritesLocalDataError()
        folders.append(FavoriteFolderSnapshot(folder_id, name, tuple(items)))
    if not folders or folders[0].folder_id != DEFAULT_FOLDER_ID:
        raise FavoritesLocalDataError()
    return FavoritesCache(uid, synced_at, tuple(folders))


def _cache_item(raw_item) -> FavoriteItemSnapshot:
    if not isinstance(raw_item, Mapping) or set(raw_item) != {
        "album_id",
        "title",
        "authors",
        "tags",
    }:
        raise FavoritesLocalDataError()
    album_id = _cache_album_id(raw_item.get("album_id"))
    title = _cache_text(raw_item.get("title"), MAX_TITLE_LENGTH)
    authors = _cache_text_tuple(raw_item.get("authors"))
    tags = _cache_text_tuple(raw_item.get("tags"))
    return FavoriteItemSnapshot(album_id, title, authors, tags)


def _remote_folder_id(value) -> str:
    try:
        return _bounded_numeric_id(value, MAX_FOLDER_ID_LENGTH)
    except ValueError:
        raise FavoritesResponseError() from None


def _remote_album_id(value) -> str:
    try:
        return str(int(normalize_album_id(value)))
    except (InvalidAlbumId, TypeError, ValueError):
        raise FavoritesResponseError() from None


def _cache_uid(value) -> str:
    try:
        return _bounded_numeric_id(value, 32)
    except ValueError:
        raise FavoritesLocalDataError() from None


def _cache_folder_id(value) -> str:
    try:
        return _bounded_numeric_id(value, MAX_FOLDER_ID_LENGTH)
    except ValueError:
        raise FavoritesLocalDataError() from None


def _cache_album_id(value) -> str:
    if not isinstance(value, str):
        raise FavoritesLocalDataError()
    try:
        normalized = str(int(normalize_album_id(value)))
    except (InvalidAlbumId, TypeError, ValueError):
        raise FavoritesLocalDataError() from None
    if normalized != value:
        raise FavoritesLocalDataError()
    return normalized


def _bounded_numeric_id(value, maximum: int) -> str:
    if type(value) is int:
        value = str(value)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ValueError
    return str(int(value))


def _remote_text(value, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise FavoritesResponseError()
    normalized = " ".join(value.split())
    if (required and not normalized) or len(normalized) > maximum:
        raise FavoritesResponseError()
    return normalized or None


def _remote_optional_text(value, maximum: int) -> str | None:
    return _remote_text(value, maximum)


def _remote_text_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise FavoritesResponseError()
    if len(values) > MAX_METADATA_VALUES:
        raise FavoritesResponseError()
    result = []
    seen = set()
    for value in values:
        normalized = _remote_text(
            value,
            MAX_METADATA_VALUE_LENGTH,
            required=True,
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _cache_text(value, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise FavoritesLocalDataError()
    normalized = " ".join(value.split())
    if value != normalized or (required and not value) or len(value) > maximum:
        raise FavoritesLocalDataError()
    return value or None


def _cache_text_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_METADATA_VALUES:
        raise FavoritesLocalDataError()
    result = []
    seen = set()
    for item in value:
        normalized = _cache_text(
            item,
            MAX_METADATA_VALUE_LENGTH,
            required=True,
        )
        if normalized in seen:
            raise FavoritesLocalDataError()
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _remote_nonnegative_int(value) -> int:
    if type(value) is int:
        result = value
    elif (
        isinstance(value, str)
        and value.strip().isascii()
        and value.strip().isdigit()
    ):
        result = int(value.strip())
    else:
        raise FavoritesResponseError()
    if result < 0:
        raise FavoritesResponseError()
    return result


def _cache_timestamp(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise FavoritesLocalDataError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FavoritesLocalDataError() from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FavoritesLocalDataError()
    normalized = _format_utc(parsed)
    if normalized != value:
        raise FavoritesLocalDataError()
    return value


def _format_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FavoritesStorageError()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _map_sync_error(error: Exception) -> FavoritesError:
    if isinstance(error, FavoritesError):
        return error
    if isinstance(error, PermissionError):
        return FavoritesSessionExpired()
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return FavoritesResponseError()
    if isinstance(
        error,
        (
            jmcomic.RequestRetryAllFailException,
            RequestException,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return FavoritesUnavailable()
    if isinstance(error, jmcomic.ResponseUnexpectedException):
        status = _response_status(error)
        if status in {401, 403}:
            return FavoritesSessionExpired()
        return FavoritesResponseError()
    if isinstance(
        error,
        (
            jmcomic.RegularNotMatchException,
            jmcomic.JsonResolveFailException,
            jmcomic.JmcomicException,
        ),
    ):
        return FavoritesResponseError()
    return FavoritesError()


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
    "FavoriteCacheStore",
    "FavoritesAccountMismatch",
    "FavoritesCache",
    "FavoritesError",
    "FavoritesLocalDataError",
    "FavoritesOperationCancelled",
    "FavoritesResponseError",
    "FavoritesService",
    "FavoritesSessionExpired",
    "FavoritesSessionRequired",
    "FavoritesStorageError",
    "FavoritesUnavailable",
]
