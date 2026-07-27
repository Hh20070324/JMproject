from datetime import datetime, timezone
import os
from pathlib import Path
import threading

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
    "MAX_READER_CHAPTER_PAGES",
    "MAX_READING_HISTORY_ENTRIES",
    "READING_HISTORY_SCHEMA_VERSION",
    "ReaderHistoryStore",
]
