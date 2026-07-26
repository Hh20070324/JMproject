from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import threading

from .protected_store import (
    ProtectedStore,
    ProtectedStoreError,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
)
from .tasks import InvalidAlbumId, normalize_album_id


SEARCH_HISTORY_SCHEMA_VERSION = 1
MAX_SEARCH_HISTORY_ENTRIES = 50
SEARCH_HISTORY_KINDS = frozenset({"keyword", "jm_id"})


@dataclass(frozen=True, slots=True)
class SearchHistoryEntry:
    kind: str
    text: str
    used_at_utc: str


class SearchHistoryStore:
    def __init__(
        self,
        protected_store: ProtectedStore,
        *,
        now=None,
    ):
        if not isinstance(protected_store, ProtectedStore):
            raise TypeError("protected_store must be ProtectedStore")
        self.protected_store = protected_store
        self._now = now or (
            lambda: datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
        )
        self._lock = threading.RLock()
        self.last_recovery_backup: Path | None = None

    def load(self) -> tuple[SearchHistoryEntry, ...]:
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
            except ProtectedStoreError:
                return ()

    def record(self, kind: str, text: str) -> tuple[SearchHistoryEntry, ...]:
        kind, text, key = self._normalize(kind, text)
        with self._lock:
            current = self.load()
            entry = SearchHistoryEntry(kind, text, self._now())
            updated = [entry]
            updated.extend(
                item
                for item in current
                if self._entry_key(item) != key
            )
            result = tuple(updated[:MAX_SEARCH_HISTORY_ENTRIES])
            self.protected_store.save(self._encode(result))
            return result

    def remove(
        self,
        kind: str,
        text: str,
    ) -> tuple[SearchHistoryEntry, ...]:
        _kind, _text, key = self._normalize(kind, text)
        with self._lock:
            current = self.load()
            result = tuple(
                entry
                for entry in current
                if self._entry_key(entry) != key
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
    def _decode(cls, payload: dict) -> tuple[SearchHistoryEntry, ...]:
        if set(payload) != {"schema_version", "entries"}:
            raise ProtectedStoreValidationError("搜索历史字段无效")
        if payload.get("schema_version") != SEARCH_HISTORY_SCHEMA_VERSION:
            raise ProtectedStoreValidationError("搜索历史版本无效")
        values = payload.get("entries")
        if (
            not isinstance(values, list)
            or len(values) > MAX_SEARCH_HISTORY_ENTRIES
        ):
            raise ProtectedStoreValidationError("搜索历史列表无效")
        entries = []
        seen = set()
        previous_time = None
        for value in values:
            if not isinstance(value, dict) or set(value) != {
                "kind",
                "text",
                "used_at_utc",
            }:
                raise ProtectedStoreValidationError("搜索历史条目无效")
            kind, text, key = cls._normalize(
                value.get("kind"),
                value.get("text"),
            )
            timestamp = value.get("used_at_utc")
            if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
                raise ProtectedStoreValidationError("搜索历史时间无效")
            try:
                parsed = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                raise ProtectedStoreValidationError(
                    "搜索历史时间无效"
                ) from None
            if parsed.tzinfo is None:
                raise ProtectedStoreValidationError("搜索历史时间无效")
            if key in seen:
                raise ProtectedStoreValidationError("搜索历史存在重复条目")
            if previous_time is not None and parsed > previous_time:
                raise ProtectedStoreValidationError("搜索历史顺序无效")
            seen.add(key)
            previous_time = parsed
            entries.append(SearchHistoryEntry(kind, text, timestamp))
        return tuple(entries)

    @staticmethod
    def _encode(entries: tuple[SearchHistoryEntry, ...]) -> dict:
        return {
            "schema_version": SEARCH_HISTORY_SCHEMA_VERSION,
            "entries": [
                {
                    "kind": entry.kind,
                    "text": entry.text,
                    "used_at_utc": entry.used_at_utc,
                }
                for entry in entries
            ],
        }

    @staticmethod
    def _normalize(
        kind: str,
        text: str,
    ) -> tuple[str, str, tuple[str, str]]:
        if kind not in SEARCH_HISTORY_KINDS or not isinstance(text, str):
            raise ProtectedStoreValidationError("搜索历史类型无效")
        if kind == "jm_id":
            try:
                normalized = str(int(normalize_album_id(text)))
            except (InvalidAlbumId, TypeError, ValueError):
                raise ProtectedStoreValidationError(
                    "搜索历史 JM 号无效"
                ) from None
            return kind, normalized, (kind, normalized)
        normalized = " ".join(text.split())
        if not normalized or len(normalized) > 500 or "\0" in normalized:
            raise ProtectedStoreValidationError("搜索历史关键词无效")
        return kind, normalized, (kind, normalized.casefold())

    @classmethod
    def _entry_key(cls, entry: SearchHistoryEntry) -> tuple[str, str]:
        return cls._normalize(entry.kind, entry.text)[2]

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
    "MAX_SEARCH_HISTORY_ENTRIES",
    "SEARCH_HISTORY_SCHEMA_VERSION",
    "SearchHistoryEntry",
    "SearchHistoryStore",
]
