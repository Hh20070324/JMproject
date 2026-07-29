import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Generic, TypeVar
import uuid
import warnings

from curl_cffi.requests.exceptions import RequestException
import jmcomic
from PIL import Image, UnidentifiedImageError

from .jmcomic_client import serialized_client_construction
from .jmcomic_logging import install_safe_jmcomic_logging
from .models import (
    ChapterCatalogSnapshot,
    ReaderChapterSnapshot,
    ReaderContentMode,
    ReaderErrorKind,
    ReaderHistoryEntry,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
)
from .option_config import apply_api_route, validate_api_route
from .protected_store import (
    ProtectedStore,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
)
from .tasks import InvalidAlbumId, normalize_album_id


READING_HISTORY_SCHEMA_VERSION = 2
MAX_READING_HISTORY_ENTRIES = 100
MAX_READER_CHAPTER_PAGES = 2_000
MAX_READER_TEXT_LENGTH = 500
DEFAULT_READER_MEMORY_BYTES = 128 * 1024 * 1024
DEFAULT_READER_DISK_BYTES = 512 * 1024 * 1024
MAX_READER_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_READER_IMAGE_SIDE = 16_384
MAX_READER_IMAGE_PIXELS = 24_000_000
READER_NETWORK_CONCURRENCY = 3
READER_NETWORK_TIMEOUT_SECONDS = 30
READER_AUTOMATIC_RETRIES = 2
_SESSION_NAME = re.compile(r"session-[0-9a-f]{32}\Z")
_SAFE_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
)
_T = TypeVar("_T")


class ReaderCacheError(Exception):
    pass


class ReaderCacheExhausted(ReaderCacheError):
    pass


class ReaderTempError(ReaderCacheError):
    pass


@dataclass(frozen=True, slots=True)
class ReaderDiskReservation:
    key: str
    page_number: int
    part_path: Path
    final_path: Path


@dataclass(slots=True)
class _DiskEntry:
    path: Path
    page_number: int
    byte_size: int
    last_used: float
    pinned: bool = False


@dataclass(slots=True)
class _MemoryEntry(Generic[_T]):
    value: _T
    page_number: int
    byte_size: int
    last_used: float
    pinned: bool = False


class ReaderMemoryCache(Generic[_T]):
    def __init__(
        self,
        budget_bytes: int = DEFAULT_READER_MEMORY_BYTES,
        *,
        clock=time.monotonic,
    ):
        _validate_budget(budget_bytes)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.budget_bytes = budget_bytes
        self._clock = clock
        self._entries: dict[str, _MemoryEntry[_T]] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def get(self, key: str) -> _T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            entry.last_used = self._clock()
            return entry.value

    def put(
        self,
        key: str,
        value: _T,
        *,
        byte_size: int,
        page_number: int,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, ...]:
        _validate_cache_key(key)
        _validate_page_number(page_number)
        _validate_page_number(current_page)
        _validate_byte_size(byte_size)
        pinned = frozenset(pinned_keys)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= old.byte_size
            self._apply_pins(pinned)
            if byte_size > self.budget_bytes:
                if old is not None:
                    self._entries[key] = old
                    self._total_bytes += old.byte_size
                raise ReaderCacheExhausted("当前页面超过内存缓存上限")
            evicted = self._evict_for(
                byte_size,
                current_page=current_page,
                protected_keys=pinned | {key},
            )
            if self._total_bytes + byte_size > self.budget_bytes:
                if old is not None:
                    self._entries[key] = old
                    self._total_bytes += old.byte_size
                raise ReaderCacheExhausted("可见页面占满内存缓存")
            self._entries[key] = _MemoryEntry(
                value=value,
                page_number=page_number,
                byte_size=byte_size,
                last_used=self._clock(),
                pinned=key in pinned,
            )
            self._total_bytes += byte_size
            return evicted

    def update_window(
        self,
        *,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, ...]:
        _validate_page_number(current_page)
        pinned = frozenset(pinned_keys)
        with self._lock:
            self._apply_pins(pinned)
            return self._evict_for(
                0,
                current_page=current_page,
                protected_keys=pinned,
            )

    def discard(self, key: str) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._total_bytes -= entry.byte_size

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def _apply_pins(self, pinned: frozenset[str]) -> None:
        for key, entry in self._entries.items():
            entry.pinned = key in pinned

    def _evict_for(
        self,
        required_bytes: int,
        *,
        current_page: int,
        protected_keys: frozenset[str],
    ) -> tuple[str, ...]:
        candidates = sorted(
            (
                (key, entry)
                for key, entry in self._entries.items()
                if not entry.pinned and key not in protected_keys
            ),
            key=lambda item: (
                -abs(item[1].page_number - current_page),
                item[1].last_used,
            ),
        )
        evicted = []
        for key, entry in candidates:
            if self._total_bytes + required_bytes <= self.budget_bytes:
                break
            self._entries.pop(key)
            self._total_bytes -= entry.byte_size
            evicted.append(key)
        return tuple(evicted)


class ReaderDiskCache:
    def __init__(
        self,
        reader_temp: Path,
        budget_bytes: int = DEFAULT_READER_DISK_BYTES,
        *,
        clock=time.monotonic,
        cleanup_stale: bool = True,
    ):
        if not isinstance(reader_temp, Path):
            raise TypeError("reader_temp must be Path")
        _validate_budget(budget_bytes)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.root = reader_temp
        self.budget_bytes = budget_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _DiskEntry] = {}
        self._total_bytes = 0
        self.cleanup_failures: tuple[Path, ...] = ()
        self._closed = False
        self._prepare_root()
        if cleanup_stale:
            self.cleanup_failures = self.cleanup_stale_sessions()
        self.session_dir = self.root / f"session-{uuid.uuid4().hex}"
        try:
            self.session_dir.mkdir()
        except OSError:
            raise ReaderTempError("无法创建在线阅读临时目录") from None
        self._validate_directory(self.session_dir, parent=self.root)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def reserve(
        self,
        page_number: int,
        suffix: str,
    ) -> ReaderDiskReservation:
        _validate_page_number(page_number)
        if not isinstance(suffix, str):
            raise ReaderTempError("临时图片格式无效")
        suffix = suffix.lower()
        if suffix not in _SAFE_IMAGE_SUFFIXES:
            raise ReaderTempError("临时图片格式不受支持")
        with self._lock:
            self._ensure_open()
            token = uuid.uuid4().hex
            final_path = self.session_dir / f"{token}{suffix}"
            return ReaderDiskReservation(
                key=token,
                page_number=page_number,
                part_path=self.session_dir / f".{token}.part{suffix}",
                final_path=final_path,
            )

    def publish(
        self,
        reservation: ReaderDiskReservation,
        *,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, tuple[str, ...]]:
        _validate_page_number(current_page)
        pinned = frozenset(pinned_keys)
        with self._lock:
            self._ensure_open()
            self._validate_reservation(reservation)
            size = self._regular_file_size(reservation.part_path)
            if size < 1:
                self._unlink_part(reservation.part_path)
                raise ReaderTempError("临时图片为空")
            if size > self.budget_bytes:
                self._unlink_part(reservation.part_path)
                raise ReaderCacheExhausted("当前页面超过磁盘缓存上限")
            self._apply_pins(pinned)
            evicted = self._evict_for(
                size,
                current_page=current_page,
                protected_keys=pinned | {reservation.key},
            )
            if self._total_bytes + size > self.budget_bytes:
                self._unlink_part(reservation.part_path)
                raise ReaderCacheExhausted("可见页面占满磁盘缓存")
            try:
                os.replace(
                    reservation.part_path,
                    reservation.final_path,
                )
            except OSError:
                self._unlink_part(reservation.part_path)
                raise ReaderTempError("临时图片无法发布") from None
            self._entries[reservation.key] = _DiskEntry(
                path=reservation.final_path,
                page_number=reservation.page_number,
                byte_size=size,
                last_used=self._clock(),
                pinned=reservation.key in pinned,
            )
            self._total_bytes += size
            return reservation.key, evicted

    def path_for(self, key: str) -> Path | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            try:
                self._regular_file_size(entry.path)
            except ReaderTempError:
                self._entries.pop(key, None)
                self._total_bytes -= entry.byte_size
                return None
            entry.last_used = self._clock()
            return entry.path

    def update_window(
        self,
        *,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, ...]:
        _validate_page_number(current_page)
        pinned = frozenset(pinned_keys)
        with self._lock:
            self._apply_pins(pinned)
            return self._evict_for(
                0,
                current_page=current_page,
                protected_keys=pinned,
            )

    def discard(self, key: str) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return
            try:
                self._unlink_regular(entry.path)
            finally:
                self._total_bytes -= entry.byte_size

    def cleanup_stale_sessions(self) -> tuple[Path, ...]:
        self._validate_directory(self.root, parent=self.root.parent)
        failures = []
        try:
            candidates = tuple(self.root.iterdir())
        except OSError:
            raise ReaderTempError("无法检查在线阅读临时目录") from None
        for candidate in candidates:
            if not _SESSION_NAME.fullmatch(candidate.name):
                continue
            try:
                self._remove_safe_tree(candidate)
            except ReaderTempError:
                failures.append(candidate)
        return tuple(failures)

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            try:
                self._remove_safe_tree(self.session_dir)
            except ReaderTempError:
                return False
            self._entries.clear()
            self._total_bytes = 0
            self._closed = True
            return True

    def _prepare_root(self) -> None:
        portable_root = self.root.parent
        self._validate_directory(portable_root, parent=portable_root.parent)
        try:
            if not self.root.exists():
                self.root.mkdir()
        except OSError:
            raise ReaderTempError("无法创建 ReaderTemp 目录") from None
        self._validate_directory(self.root, parent=portable_root)

    def _validate_reservation(
        self,
        reservation: ReaderDiskReservation,
    ) -> None:
        if not isinstance(reservation, ReaderDiskReservation):
            raise ReaderTempError("临时图片预留无效")
        if not re.fullmatch(r"[0-9a-f]{32}", reservation.key):
            raise ReaderTempError("临时图片预留无效")
        _validate_page_number(reservation.page_number)
        suffix = reservation.final_path.suffix.lower()
        expected_final = self.session_dir / f"{reservation.key}{suffix}"
        expected_part = (
            self.session_dir / f".{reservation.key}.part{suffix}"
        )
        if (
            suffix not in _SAFE_IMAGE_SUFFIXES
            or reservation.final_path != expected_final
            or reservation.part_path != expected_part
        ):
            raise ReaderTempError("临时图片路径无效")
        self._validate_directory(self.session_dir, parent=self.root)

    def _regular_file_size(self, path: Path) -> int:
        try:
            state = path.lstat()
        except OSError:
            raise ReaderTempError("临时图片无法访问") from None
        if not stat.S_ISREG(state.st_mode) or _is_reparse_state(state):
            raise ReaderTempError("临时图片不是普通文件")
        try:
            if path.resolve(strict=True).parent != self.session_dir.resolve(
                strict=True
            ):
                raise ReaderTempError("临时图片超出会话目录")
        except OSError:
            raise ReaderTempError("临时图片路径无法解析") from None
        return state.st_size

    def _apply_pins(self, pinned: frozenset[str]) -> None:
        for key, entry in self._entries.items():
            entry.pinned = key in pinned

    def _evict_for(
        self,
        required_bytes: int,
        *,
        current_page: int,
        protected_keys: frozenset[str],
    ) -> tuple[str, ...]:
        candidates = sorted(
            (
                (key, entry)
                for key, entry in self._entries.items()
                if not entry.pinned and key not in protected_keys
            ),
            key=lambda item: (
                -abs(item[1].page_number - current_page),
                item[1].last_used,
            ),
        )
        evicted = []
        for key, entry in candidates:
            if self._total_bytes + required_bytes <= self.budget_bytes:
                break
            self._unlink_regular(entry.path)
            self._entries.pop(key)
            self._total_bytes -= entry.byte_size
            evicted.append(key)
        return tuple(evicted)

    def _remove_safe_tree(self, target: Path) -> None:
        self._validate_directory(target, parent=self.root)
        directories = [target]
        files = []
        scan = [target]
        while scan:
            directory = scan.pop()
            try:
                entries = tuple(os.scandir(directory))
            except OSError:
                raise ReaderTempError("临时会话目录无法扫描") from None
            for entry in entries:
                try:
                    state = entry.stat(follow_symlinks=False)
                except OSError:
                    raise ReaderTempError("临时会话内容无法检查") from None
                if _is_reparse_state(state):
                    raise ReaderTempError("临时会话包含链接")
                path = Path(entry.path)
                if stat.S_ISDIR(state.st_mode):
                    directories.append(path)
                    scan.append(path)
                elif stat.S_ISREG(state.st_mode):
                    files.append(path)
                else:
                    raise ReaderTempError("临时会话包含非常规文件")
        try:
            for file_path in files:
                file_path.unlink()
            for directory in reversed(directories):
                directory.rmdir()
        except OSError:
            raise ReaderTempError("临时会话清理失败") from None

    @staticmethod
    def _validate_directory(path: Path, *, parent: Path) -> None:
        try:
            state = path.lstat()
            parent_resolved = parent.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except OSError:
            raise ReaderTempError("在线阅读临时目录无法访问") from None
        if not stat.S_ISDIR(state.st_mode) or _is_reparse_state(state):
            raise ReaderTempError("在线阅读临时目录不是普通目录")
        if resolved.parent != parent_resolved:
            raise ReaderTempError("在线阅读临时目录越界")

    @staticmethod
    def _unlink_regular(path: Path) -> None:
        try:
            state = path.lstat()
            if not stat.S_ISREG(state.st_mode) or _is_reparse_state(state):
                raise ReaderTempError("拒绝删除非常规临时文件")
            path.unlink()
        except ReaderTempError:
            raise
        except OSError:
            raise ReaderTempError("临时文件删除失败") from None

    @staticmethod
    def _unlink_part(path: Path) -> None:
        try:
            state = path.lstat()
            if stat.S_ISREG(state.st_mode) and not _is_reparse_state(state):
                path.unlink()
        except OSError:
            return

    def _ensure_open(self) -> None:
        if self._closed:
            raise ReaderTempError("在线阅读缓存已经关闭")


def _validate_budget(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("cache budget must be a positive integer")


def _validate_byte_size(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("byte_size must be a positive integer")


def _validate_page_number(value: int) -> None:
    if (
        type(value) is not int
        or not 1 <= value <= MAX_READER_CHAPTER_PAGES
    ):
        raise ValueError("page number is out of range")


def _validate_cache_key(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("cache key is invalid")


def _is_reparse_state(state) -> bool:
    attributes = getattr(state, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


class ReaderServiceError(Exception):
    def __init__(
        self,
        kind: ReaderErrorKind,
        message: str,
    ):
        if not isinstance(kind, ReaderErrorKind):
            raise TypeError("kind must be ReaderErrorKind")
        super().__init__(message)
        self.kind = kind


class ReaderService:
    def __init__(
        self,
        *,
        option_file: Path,
        disk_cache: ReaderDiskCache,
        api_route_provider: Callable[[], str] | None = None,
        session_cookie_provider: Callable[
            [], Mapping[str, str] | None
        ] | None = None,
        session_expired_callback: Callable[[], object] | None = None,
        client_factory: Callable[
            [dict[str, str] | None, str], object
        ] | None = None,
        max_response_bytes: int = MAX_READER_RESPONSE_BYTES,
        max_image_side: int = MAX_READER_IMAGE_SIDE,
        max_image_pixels: int = MAX_READER_IMAGE_PIXELS,
        automatic_retries: int = READER_AUTOMATIC_RETRIES,
        retry_delays: tuple[float, ...] = (0.1, 0.3),
    ):
        if not isinstance(option_file, Path):
            raise TypeError("option_file must be Path")
        if not isinstance(disk_cache, ReaderDiskCache):
            raise TypeError("disk_cache must be ReaderDiskCache")
        for callback, name in (
            (api_route_provider, "api_route_provider"),
            (session_cookie_provider, "session_cookie_provider"),
            (session_expired_callback, "session_expired_callback"),
            (client_factory, "client_factory"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable")
        for value, name in (
            (max_response_bytes, "max_response_bytes"),
            (max_image_side, "max_image_side"),
            (max_image_pixels, "max_image_pixels"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(automatic_retries) is not int
            or not 0 <= automatic_retries <= READER_AUTOMATIC_RETRIES
        ):
            raise ValueError("automatic_retries is out of range")
        if (
            not isinstance(retry_delays, tuple)
            or len(retry_delays) < automatic_retries
            or any(
                not isinstance(delay, (int, float)) or delay < 0
                for delay in retry_delays
            )
        ):
            raise ValueError("retry_delays is invalid")
        self.option_file = option_file
        self.disk_cache = disk_cache
        self._api_route_provider = api_route_provider or (lambda: "auto")
        self._session_cookie_provider = session_cookie_provider
        self._session_expired_callback = session_expired_callback
        self._client_factory = client_factory or self._build_client
        self.max_response_bytes = max_response_bytes
        self.max_image_side = max_image_side
        self.max_image_pixels = max_image_pixels
        self.automatic_retries = automatic_retries
        self.retry_delays = retry_delays
        self._client = None
        self._authenticated = False
        self._anonymous_fallback_used = False
        self._route = "auto"
        self._photos: dict[str, object] = {}
        self._page_disk_keys: dict[tuple[str, int], str] = {}
        self._page_snapshots: dict[
            tuple[str, int], ReaderPageSnapshot
        ] = {}
        self._client_lock: asyncio.Lock | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise ReaderServiceError(
                ReaderErrorKind.INTERNAL,
                "在线阅读服务已经关闭",
            )
        if self._client is not None:
            return
        self._route = self._current_route()
        cookies = self._current_cookies()
        self._client_lock = asyncio.Lock()
        try:
            self._client = self._client_factory(cookies, self._route)
            if self._client is None:
                raise TypeError("empty client")
            await self._setup_client(self._client, self._route)
            self._authenticated = bool(cookies)
        except ReaderServiceError:
            raise
        except Exception as error:
            await self._close_client(self._client)
            self._client = None
            raise self._map_error(error) from None

    async def close(self) -> bool:
        if self._closed:
            return True
        self._closed = True
        client = self._client
        self._client = None
        self._photos.clear()
        self._page_disk_keys.clear()
        self._page_snapshots.clear()
        client_closed = await self._close_client(client)
        cache_closed = await asyncio.to_thread(self.disk_cache.close)
        return client_closed and cache_closed

    async def fetch_catalog(
        self,
        album_id: str,
    ) -> ChapterCatalogSnapshot:
        normalized_id = self._normalize_id(album_id, "漫画 JM 号")
        album = await self._call_with_policy(
            lambda client: client.get_album_detail(normalized_id)
        )
        try:
            from .search import _snapshot_chapter_catalog

            catalog = _snapshot_chapter_catalog(
                album,
                expected_id=normalized_id,
            )
        except Exception as error:
            if isinstance(error, ReaderServiceError):
                raise
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "章节目录暂时无法读取",
            ) from None
        if len(catalog.chapters) > MAX_READER_CHAPTER_PAGES:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "章节数量超过在线阅读安全上限",
            )
        return catalog

    async def load_chapter(
        self,
        catalog: ChapterCatalogSnapshot,
        photo_id: str,
    ) -> tuple[
        ReaderChapterSnapshot,
        tuple[ReaderPageSnapshot, ...],
    ]:
        if not isinstance(catalog, ChapterCatalogSnapshot):
            raise TypeError("catalog must be ChapterCatalogSnapshot")
        normalized_id = self._normalize_id(photo_id, "章节 JM 号")
        chapter = next(
            (
                item
                for item in catalog.chapters
                if item.photo_id == normalized_id
            ),
            None,
        )
        if chapter is None:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "所选章节已不在远端目录中",
            )
        photo = await self._call_with_policy(
            lambda client: client.get_photo_detail(
                normalized_id,
                fetch_album=False,
                fetch_scramble_id=True,
            )
        )
        try:
            actual_photo_id = self._normalize_id(
                getattr(photo, "photo_id", None),
                "章节 JM 号",
            )
            raw_pages = getattr(photo, "page_arr", None)
            if (
                actual_photo_id != normalized_id
                or isinstance(raw_pages, (str, bytes, bytearray, Mapping))
            ):
                raise ValueError("invalid photo")
            pages = tuple(raw_pages)
            if not pages or len(pages) > MAX_READER_CHAPTER_PAGES:
                raise ValueError("invalid page count")
            for index in range(len(pages)):
                detail = photo.create_image_detail(index)
                if (
                    not isinstance(getattr(detail, "download_url", None), str)
                    or not detail.download_url
                ):
                    raise ValueError("invalid image detail")
        except ReaderServiceError:
            raise
        except Exception:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "章节页面列表无效或超出安全上限",
            ) from None
        self._photos[normalized_id] = photo
        total = len(pages)
        chapter_snapshot = ReaderChapterSnapshot(
            normalized_id,
            chapter.index,
            chapter.title,
            total,
        )
        page_snapshots = tuple(
            ReaderPageSnapshot(
                normalized_id,
                page_number,
                total,
                ReaderPageState.PLACEHOLDER,
            )
            for page_number in range(1, total + 1)
        )
        return chapter_snapshot, page_snapshots

    async def fetch_page(
        self,
        photo_id: str,
        page_number: int,
        *,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, ReaderPageSnapshot]:
        normalized_id = self._normalize_id(photo_id, "章节 JM 号")
        _validate_page_number(page_number)
        _validate_page_number(current_page)
        photo = self._photos.get(normalized_id)
        if photo is None:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "章节尚未加载",
            )
        try:
            page_count = len(photo.page_arr)
            if page_number > page_count:
                raise ValueError("page out of range")
            page_identity = (normalized_id, page_number)
            cached_key = self._page_disk_keys.get(page_identity)
            cached_snapshot = self._page_snapshots.get(page_identity)
            if cached_key is not None and cached_snapshot is not None:
                cached_path = self.disk_cache.path_for(cached_key)
                if cached_path is not None:
                    return cached_key, cached_snapshot
                self._page_disk_keys.pop(page_identity, None)
                self._page_snapshots.pop(page_identity, None)
            image = photo.create_image_detail(page_number - 1)
            image_url = image.download_url
            suffix = str(getattr(image, "img_file_suffix", "")).lower()
            if suffix not in _SAFE_IMAGE_SUFFIXES:
                raise ReaderServiceError(
                    ReaderErrorKind.IMAGE_DAMAGED,
                    "图片格式不受支持",
                )
        except ReaderServiceError:
            raise
        except Exception:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "图片页码已失效",
            ) from None
        response = await self._call_with_policy(
            lambda client: client.get_jm_image(image_url),
            image_request=True,
        )
        content = getattr(response, "content", None)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "图片响应无效",
            )
        response_bytes = bytes(content)
        if not response_bytes:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_EMPTY,
                "图片响应为空",
            )
        if len(response_bytes) > self.max_response_bytes:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_TOO_LARGE,
                "图片响应超过安全上限",
            )
        reservation = self.disk_cache.reserve(page_number, suffix)
        try:
            width, height = await asyncio.to_thread(
                self._decode_validate_and_stage,
                response,
                image,
                reservation,
            )
            page_cache_key = f"{normalized_id}:{page_number}"
            disk_pins = {
                self._page_disk_keys[value]
                for value in self._page_disk_keys
                if (
                    f"{value[0]}:{value[1]}" in pinned_keys
                    and self._page_disk_keys[value]
                )
            }
            if page_cache_key in pinned_keys:
                disk_pins.add(reservation.key)
            key, evicted = await asyncio.to_thread(
                self.disk_cache.publish,
                reservation,
                current_page=current_page,
                pinned_keys=disk_pins,
            )
        except ReaderServiceError:
            self.disk_cache._unlink_part(reservation.part_path)
            raise
        except ReaderCacheExhausted:
            raise ReaderServiceError(
                ReaderErrorKind.CACHE_EXHAUSTED,
                "在线阅读缓存空间不足",
            ) from None
        except ReaderTempError:
            raise ReaderServiceError(
                ReaderErrorKind.TEMP_UNAVAILABLE,
                "在线阅读临时目录不可用",
            ) from None
        except Exception:
            self.disk_cache._unlink_part(reservation.part_path)
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DECODE_FAILED,
                "图片还原失败",
            ) from None
        snapshot = ReaderPageSnapshot(
            normalized_id,
            page_number,
            page_count,
            ReaderPageState.READY,
            width=width,
            height=height,
            cache_path=reservation.final_path,
        )
        self._page_disk_keys[(normalized_id, page_number)] = key
        self._page_snapshots[(normalized_id, page_number)] = snapshot
        if evicted:
            evicted_set = frozenset(evicted)
            for identity, disk_key in tuple(self._page_disk_keys.items()):
                if disk_key in evicted_set:
                    self._page_disk_keys.pop(identity, None)
                    self._page_snapshots.pop(identity, None)
        return key, snapshot

    def update_cache_window(
        self,
        photo_id: str,
        *,
        current_page: int,
        visible_pages,
    ) -> tuple[str, ...]:
        normalized_id = self._normalize_id(photo_id, "章节 JM 号")
        _validate_page_number(current_page)
        visible = {
            (normalized_id, page)
            for page in visible_pages
            if (
                type(page) is int
                and 1 <= page <= MAX_READER_CHAPTER_PAGES
            )
        }
        pinned = {
            self._page_disk_keys[identity]
            for identity in visible
            if identity in self._page_disk_keys
        }
        evicted = self.disk_cache.update_window(
            current_page=current_page,
            pinned_keys=pinned,
        )
        if evicted:
            evicted_set = frozenset(evicted)
            for identity, disk_key in tuple(self._page_disk_keys.items()):
                if disk_key in evicted_set:
                    self._page_disk_keys.pop(identity, None)
                    self._page_snapshots.pop(identity, None)
        return evicted

    def _decode_validate_and_stage(
        self,
        response,
        image,
        reservation: ReaderDiskReservation,
    ) -> tuple[int, int]:
        try:
            response.transfer_to(
                str(reservation.part_path),
                scramble_id=image.scramble_id,
                decode_image=True,
                img_url=image.download_url,
            )
        except OSError:
            raise ReaderTempError("图片无法写入临时目录") from None
        except Exception:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DECODE_FAILED,
                "图片还原失败",
            ) from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(reservation.part_path) as decoded:
                    width, height = decoded.size
                    if (
                        type(width) is not int
                        or type(height) is not int
                        or width < 1
                        or height < 1
                    ):
                        raise ValueError("invalid image dimensions")
                    if (
                        width > self.max_image_side
                        or height > self.max_image_side
                        or width * height > self.max_image_pixels
                    ):
                        raise ReaderServiceError(
                            ReaderErrorKind.IMAGE_DIMENSIONS_EXCEEDED,
                            "图片尺寸超过安全上限",
                        )
                    decoded.verify()
        except ReaderServiceError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DIMENSIONS_EXCEEDED,
                "图片尺寸超过安全上限",
            ) from None
        except (UnidentifiedImageError, OSError, ValueError):
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "图片内容损坏",
            ) from None
        return width, height

    async def _call_with_policy(
        self,
        operation: Callable[[object], Awaitable],
        *,
        image_request: bool = False,
    ):
        await self.start()
        fallback_attempted = False
        attempt = 0
        while True:
            client = self._client
            try:
                return await operation(client)
            except Exception as error:
                mapped = self._map_error(error, image_request=image_request)
                if (
                    mapped.kind is ReaderErrorKind.SESSION_EXPIRED
                    and self._authenticated
                    and not self._anonymous_fallback_used
                    and not fallback_attempted
                ):
                    fallback_attempted = True
                    await self._fallback_to_anonymous()
                    continue
                if (
                    mapped.kind
                    in {
                        ReaderErrorKind.NETWORK_UNAVAILABLE,
                        ReaderErrorKind.ROUTE_UNAVAILABLE,
                    }
                    and attempt < self.automatic_retries
                ):
                    delay = self.retry_delays[attempt]
                    attempt += 1
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                raise mapped from None

    async def _fallback_to_anonymous(self) -> None:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if self._anonymous_fallback_used:
                return
            old_client = self._client
            route = self._current_route()
            try:
                replacement = self._client_factory(None, route)
                await self._setup_client(replacement, route)
            except Exception as error:
                await self._close_client(locals().get("replacement"))
                raise self._map_error(error) from None
            self._client = replacement
            self._authenticated = False
            self._anonymous_fallback_used = True
            await self._close_client(old_client)
            if self._session_expired_callback is not None:
                try:
                    self._session_expired_callback()
                except Exception:
                    pass

    def _build_client(
        self,
        cookies: dict[str, str] | None,
        route: str,
    ):
        install_safe_jmcomic_logging()
        with serialized_client_construction():
            option = jmcomic.create_option_by_file(str(self.option_file))
            apply_api_route(option, route)
            option.client.retry_times = 0
            option.client.src_dict["timeout"] = (
                READER_NETWORK_TIMEOUT_SECONDS
            )
            if cookies:
                option.update_cookies(dict(cookies))
            return option.new_jm_async_client(
                max_clients=READER_NETWORK_CONCURRENCY,
            )

    @staticmethod
    async def _setup_client(client, route: str) -> None:
        setup = getattr(client, "setup", None)
        if callable(setup):
            result = setup()
            if inspect_is_awaitable(result):
                await result
        if route != "auto":
            setter = getattr(client, "set_domain_list", None)
            if not callable(setter):
                raise TypeError("fixed route client cannot set domain")
            setter([route])

    @staticmethod
    async def _close_client(client) -> bool:
        if client is None:
            return True
        close = getattr(client, "close", None)
        if not callable(close):
            return False
        try:
            result = close()
            if inspect_is_awaitable(result):
                await result
            return True
        except Exception:
            return False

    def _current_route(self) -> str:
        try:
            return validate_api_route(self._api_route_provider())
        except Exception:
            raise ReaderServiceError(
                ReaderErrorKind.ROUTE_UNAVAILABLE,
                "API 路线设置无效",
            ) from None

    def _current_cookies(self) -> dict[str, str] | None:
        if self._session_cookie_provider is None:
            return None
        try:
            values = self._session_cookie_provider()
        except Exception:
            return None
        if values is None:
            return None
        if not isinstance(values, Mapping):
            return None
        result = {}
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or len(key) > 128
                or len(value) > 4096
                or "\0" in key
                or "\0" in value
            ):
                continue
            result[key] = value
        return result or None

    def _map_error(
        self,
        error: Exception,
        *,
        image_request: bool = False,
    ) -> ReaderServiceError:
        if isinstance(error, ReaderServiceError):
            return error
        status = _response_status(error)
        if status in {401, 403} or isinstance(error, PermissionError):
            return ReaderServiceError(
                ReaderErrorKind.SESSION_EXPIRED,
                "登录已失效或当前内容不可用",
            )
        name = type(error).__name__.casefold()
        text = str(error).casefold()
        if (
            status == 404
            or "missingalbum" in name
            or "missingphoto" in name
            or "not found" in text
        ):
            return ReaderServiceError(
                ReaderErrorKind.NOT_FOUND,
                "漫画或章节不存在",
            )
        if isinstance(
            error,
            (TimeoutError, ConnectionError, OSError, RequestException),
        ):
            kind = (
                ReaderErrorKind.ROUTE_UNAVAILABLE
                if self._route != "auto"
                else ReaderErrorKind.NETWORK_UNAVAILABLE
            )
            return ReaderServiceError(
                kind,
                (
                    "固定 API 路线不可用，请切回自动选择"
                    if kind is ReaderErrorKind.ROUTE_UNAVAILABLE
                    else "网络暂时不可用"
                ),
            )
        if image_request:
            return ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "图片请求失败",
            )
        return ReaderServiceError(
            ReaderErrorKind.INTERNAL,
            "在线阅读暂时失败",
        )

    @staticmethod
    def _normalize_id(value, label: str) -> str:
        try:
            return str(int(normalize_album_id(value)))
        except (InvalidAlbumId, TypeError, ValueError):
            raise ReaderServiceError(
                ReaderErrorKind.NOT_FOUND,
                f"{label}无效",
            ) from None


def inspect_is_awaitable(value) -> bool:
    return hasattr(value, "__await__")


def _response_status(error: Exception) -> int | None:
    for target in (
        error,
        getattr(error, "response", None),
        getattr(error, "resp", None),
    ):
        if target is None:
            continue
        for name in ("status_code", "http_code", "status"):
            value = getattr(target, name, None)
            if type(value) is int:
                return value
    return None


class ReaderHistoryStore:
    def __init__(
        self,
        protected_store: ProtectedStore,
        *,
        now=None,
    ):
        if not isinstance(protected_store, ProtectedStore):
            raise TypeError("protected_store must be ProtectedStore")
        if protected_store.kind.value != "reading_history":
            raise TypeError("protected_store must be reading history")
        self.protected_store = protected_store
        self._now = now or (
            lambda: datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
        )
        self._lock = threading.RLock()
        self.last_recovery_backup: Path | None = None

    def load(self) -> tuple[ReaderHistoryEntry, ...]:
        with self._lock:
            self.last_recovery_backup = None
            try:
                payload = self.protected_store.load()
                if payload is None:
                    return ()
                return self._decode(payload)
            except (
                ProtectedStoreUnreadableError,
                ProtectedStoreValidationError,
            ):
                self._backup_unreadable()
                return ()

    def find(self, album_id: str) -> ReaderHistoryEntry | None:
        normalized_id = self._normalize_id(album_id, "漫画 JM 号")
        with self._lock:
            return next(
                (
                    entry
                    for entry in self.load()
                    if entry.album_id == normalized_id
                ),
                None,
            )

    def record(
        self,
        *,
        album_id: str,
        title: str,
        photo_id: str,
        chapter_title: str,
        chapter_index: int,
        page_number: int,
        page_count: int,
        source: ReaderSource,
        content_mode: ReaderContentMode = ReaderContentMode.ONLINE,
    ) -> tuple[ReaderHistoryEntry, ...]:
        entry = self._normalize_entry(
            ReaderHistoryEntry(
                album_id=album_id,
                title=title,
                photo_id=photo_id,
                chapter_title=chapter_title,
                chapter_index=chapter_index,
                page_number=page_number,
                page_count=page_count,
                read_at_utc=self._now(),
                source=source,
                content_mode=content_mode,
            )
        )
        with self._lock:
            current = self.load()
            updated = (entry,) + tuple(
                item
                for item in current
                if item.album_id != entry.album_id
            )
            result = updated[:MAX_READING_HISTORY_ENTRIES]
            self.protected_store.save(self._encode(result))
            return result

    def remove(self, album_id: str) -> tuple[ReaderHistoryEntry, ...]:
        normalized_id = self._normalize_id(album_id, "漫画 JM 号")
        with self._lock:
            current = self.load()
            result = tuple(
                entry
                for entry in current
                if entry.album_id != normalized_id
            )
            if result == current:
                return current
            if result:
                self.protected_store.save(self._encode(result))
            else:
                self.protected_store.delete()
            return result

    def clear(self) -> None:
        with self._lock:
            self.protected_store.delete()

    @classmethod
    def _decode(cls, payload: dict) -> tuple[ReaderHistoryEntry, ...]:
        if set(payload) != {"schema_version", "entries"}:
            raise ProtectedStoreValidationError("阅读历史字段无效")
        schema_version = payload.get("schema_version")
        if schema_version not in {1, READING_HISTORY_SCHEMA_VERSION}:
            raise ProtectedStoreValidationError("阅读历史版本无效")
        values = payload.get("entries")
        if (
            not isinstance(values, list)
            or len(values) > MAX_READING_HISTORY_ENTRIES
        ):
            raise ProtectedStoreValidationError("阅读历史列表无效")
        entries = []
        seen = set()
        previous_time = None
        fields = {
            "album_id",
            "title",
            "photo_id",
            "chapter_title",
            "chapter_index",
            "page_number",
            "page_count",
            "read_at_utc",
            "source",
        }
        if schema_version == READING_HISTORY_SCHEMA_VERSION:
            fields.add("content_mode")
        for value in values:
            if not isinstance(value, dict) or set(value) != fields:
                raise ProtectedStoreValidationError("阅读历史条目无效")
            try:
                source = ReaderSource(value.get("source"))
            except (TypeError, ValueError):
                raise ProtectedStoreValidationError(
                    "阅读历史来源无效"
                ) from None
            try:
                content_mode = (
                    ReaderContentMode(value.get("content_mode"))
                    if schema_version == READING_HISTORY_SCHEMA_VERSION
                    else ReaderContentMode.ONLINE
                )
            except (TypeError, ValueError):
                raise ProtectedStoreValidationError(
                    "阅读历史内容来源无效"
                ) from None
            entry = cls._normalize_entry(
                ReaderHistoryEntry(
                    album_id=value.get("album_id"),
                    title=value.get("title"),
                    photo_id=value.get("photo_id"),
                    chapter_title=value.get("chapter_title"),
                    chapter_index=value.get("chapter_index"),
                    page_number=value.get("page_number"),
                    page_count=value.get("page_count"),
                    read_at_utc=value.get("read_at_utc"),
                    source=source,
                    content_mode=content_mode,
                )
            )
            parsed_time = cls._parse_timestamp(entry.read_at_utc)
            if entry.album_id in seen:
                raise ProtectedStoreValidationError("阅读历史漫画重复")
            if previous_time is not None and parsed_time > previous_time:
                raise ProtectedStoreValidationError("阅读历史顺序无效")
            seen.add(entry.album_id)
            previous_time = parsed_time
            entries.append(entry)
        return tuple(entries)

    @staticmethod
    def _encode(entries: tuple[ReaderHistoryEntry, ...]) -> dict:
        return {
            "schema_version": READING_HISTORY_SCHEMA_VERSION,
            "entries": [
                {
                    "album_id": entry.album_id,
                    "title": entry.title,
                    "photo_id": entry.photo_id,
                    "chapter_title": entry.chapter_title,
                    "chapter_index": entry.chapter_index,
                    "page_number": entry.page_number,
                    "page_count": entry.page_count,
                    "read_at_utc": entry.read_at_utc,
                    "source": entry.source.value,
                    "content_mode": entry.content_mode.value,
                }
                for entry in entries
            ],
        }

    @classmethod
    def _normalize_entry(
        cls,
        entry: ReaderHistoryEntry,
    ) -> ReaderHistoryEntry:
        if not isinstance(entry, ReaderHistoryEntry):
            raise ProtectedStoreValidationError("阅读历史条目无效")
        album_id = cls._normalize_id(entry.album_id, "漫画 JM 号")
        photo_id = cls._normalize_id(entry.photo_id, "章节 JM 号")
        title = cls._normalize_text(entry.title, "漫画标题")
        chapter_title = cls._normalize_text(
            entry.chapter_title,
            "章节标题",
        )
        if (
            type(entry.chapter_index) is not int
            or not 1 <= entry.chapter_index <= MAX_READER_CHAPTER_PAGES
        ):
            raise ProtectedStoreValidationError("阅读历史章节序号无效")
        if (
            type(entry.page_count) is not int
            or not 1 <= entry.page_count <= MAX_READER_CHAPTER_PAGES
        ):
            raise ProtectedStoreValidationError("阅读历史总页数无效")
        if (
            type(entry.page_number) is not int
            or not 1 <= entry.page_number <= entry.page_count
        ):
            raise ProtectedStoreValidationError("阅读历史页码无效")
        if not isinstance(entry.source, ReaderSource):
            raise ProtectedStoreValidationError("阅读历史来源无效")
        if not isinstance(entry.content_mode, ReaderContentMode):
            raise ProtectedStoreValidationError("阅读历史内容来源无效")
        cls._parse_timestamp(entry.read_at_utc)
        return ReaderHistoryEntry(
            album_id=album_id,
            title=title,
            photo_id=photo_id,
            chapter_title=chapter_title,
            chapter_index=entry.chapter_index,
            page_number=entry.page_number,
            page_count=entry.page_count,
            read_at_utc=entry.read_at_utc,
            source=entry.source,
            content_mode=entry.content_mode,
        )

    @staticmethod
    def _normalize_id(value, label: str) -> str:
        try:
            return str(int(normalize_album_id(value)))
        except (InvalidAlbumId, TypeError, ValueError):
            raise ProtectedStoreValidationError(
                f"阅读历史{label}无效"
            ) from None

    @staticmethod
    def _normalize_text(value, label: str) -> str:
        if not isinstance(value, str):
            raise ProtectedStoreValidationError(f"阅读历史{label}无效")
        normalized = " ".join(value.split())
        if (
            not normalized
            or len(normalized) > MAX_READER_TEXT_LENGTH
            or "\0" in normalized
        ):
            raise ProtectedStoreValidationError(f"阅读历史{label}无效")
        return normalized

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ProtectedStoreValidationError("阅读历史时间无效")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ProtectedStoreValidationError("阅读历史时间无效") from None
        if parsed.tzinfo is None:
            raise ProtectedStoreValidationError("阅读历史时间无效")
        return parsed

    def _backup_unreadable(self) -> None:
        source = self.protected_store.path
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
    "DEFAULT_READER_DISK_BYTES",
    "DEFAULT_READER_MEMORY_BYTES",
    "MAX_READER_IMAGE_PIXELS",
    "MAX_READER_IMAGE_SIDE",
    "MAX_READER_CHAPTER_PAGES",
    "MAX_READER_RESPONSE_BYTES",
    "MAX_READING_HISTORY_ENTRIES",
    "READER_AUTOMATIC_RETRIES",
    "READER_NETWORK_CONCURRENCY",
    "READER_NETWORK_TIMEOUT_SECONDS",
    "READING_HISTORY_SCHEMA_VERSION",
    "ReaderCacheError",
    "ReaderCacheExhausted",
    "ReaderDiskCache",
    "ReaderDiskReservation",
    "ReaderHistoryStore",
    "ReaderMemoryCache",
    "ReaderService",
    "ReaderServiceError",
    "ReaderTempError",
]
