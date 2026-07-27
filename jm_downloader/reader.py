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

from .models import ReaderHistoryEntry, ReaderSource
from .protected_store import (
    ProtectedStore,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
)
from .tasks import InvalidAlbumId, normalize_album_id


READING_HISTORY_SCHEMA_VERSION = 1
MAX_READING_HISTORY_ENTRIES = 100
MAX_READER_CHAPTER_PAGES = 2_000
MAX_READER_TEXT_LENGTH = 500
DEFAULT_READER_MEMORY_BYTES = 128 * 1024 * 1024
DEFAULT_READER_DISK_BYTES = 512 * 1024 * 1024
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
                part_path=self.session_dir / f".{token}{suffix}.part",
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
            self.session_dir / f".{reservation.key}{suffix}.part"
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
        if payload.get("schema_version") != READING_HISTORY_SCHEMA_VERSION:
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
        for value in values:
            if not isinstance(value, dict) or set(value) != fields:
                raise ProtectedStoreValidationError("阅读历史条目无效")
            try:
                source = ReaderSource(value.get("source"))
            except (TypeError, ValueError):
                raise ProtectedStoreValidationError(
                    "阅读历史来源无效"
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
    "MAX_READER_CHAPTER_PAGES",
    "MAX_READING_HISTORY_ENTRIES",
    "READING_HISTORY_SCHEMA_VERSION",
    "ReaderCacheError",
    "ReaderCacheExhausted",
    "ReaderDiskCache",
    "ReaderDiskReservation",
    "ReaderHistoryStore",
    "ReaderMemoryCache",
    "ReaderTempError",
]
