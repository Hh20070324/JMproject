from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .models import TaskStatus
from .settings import (
    AppPaths,
    DEFAULT_PATHS,
    SettingsValidationError,
    resolve_portable_path,
    serialize_portable_path,
    validate_portable_directory,
)


TASK_STORE_SCHEMA_VERSION = 2
PERSISTED_TASK_STATUSES = {
    TaskStatus.PENDING,
    TaskStatus.FETCHING,
    TaskStatus.DOWNLOADING,
    TaskStatus.PAUSING,
    TaskStatus.PAUSED,
    TaskStatus.CANCELLING,
    TaskStatus.FAILED,
}


class TaskStoreError(Exception):
    pass


class TaskStoreValidationError(TaskStoreError):
    pass


class TaskStoreRecoveryError(TaskStoreError):
    pass


class UnsupportedTaskStoreVersion(TaskStoreError):
    pass


@dataclass(frozen=True, slots=True)
class StoredTask:
    id: str
    album_id: str
    title: str | None
    status: TaskStatus
    progress: int
    chapter: str
    page: str
    error: str | None
    pictures_directory: str
    pdf_directory: str
    selected_chapter_ids: tuple[str, ...] | None = None

    def validate(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or len(self.id) > 64
            or not self.id.isascii()
            or not self.id.replace("-", "").isalnum()
        ):
            raise TaskStoreValidationError("任务 ID 无效")
        if (
            not isinstance(self.album_id, str)
            or not self.album_id.isascii()
            or not self.album_id.isdigit()
        ):
            raise TaskStoreValidationError("任务漫画编号无效")
        if self.status not in PERSISTED_TASK_STATUSES:
            raise TaskStoreValidationError("任务状态不可恢复")
        if type(self.progress) is not int or not 0 <= self.progress <= 100:
            raise TaskStoreValidationError("任务进度必须在 0 到 100 之间")
        self._validate_optional_text("任务标题", self.title)
        self._validate_text("章节摘要", self.chapter)
        self._validate_text("页数摘要", self.page)
        self._validate_optional_text("任务错误", self.error)
        self._validate_selected_chapter_ids()
        try:
            validate_portable_directory("任务图片目录", self.pictures_directory)
            validate_portable_directory("任务 PDF 目录", self.pdf_directory)
        except SettingsValidationError as error:
            raise TaskStoreValidationError(str(error)) from error

    def to_dict(self) -> dict:
        self.validate()
        return {
            "id": self.id,
            "album_id": self.album_id,
            "title": self.title,
            "status": self.status.value,
            "progress": self.progress,
            "chapter": self.chapter,
            "page": self.page,
            "error": self.error,
            "selected_chapter_ids": (
                list(self.selected_chapter_ids)
                if self.selected_chapter_ids is not None
                else None
            ),
            "paths": {
                "pictures": self.pictures_directory,
                "pdfs": self.pdf_directory,
            },
        }

    def to_paths(self, portable_root: Path) -> AppPaths:
        self.validate()
        root = Path(portable_root).resolve()
        return AppPaths(
            root=root,
            pictures_override=resolve_portable_path(
                root,
                self.pictures_directory,
            ),
            pdfs_override=resolve_portable_path(
                root,
                self.pdf_directory,
            ),
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping,
        *,
        legacy_schema: bool = False,
    ) -> "StoredTask":
        if not isinstance(data, Mapping):
            raise TaskStoreValidationError("任务记录必须是对象")
        paths = data.get("paths")
        if not isinstance(paths, Mapping):
            raise TaskStoreValidationError("任务目录必须是对象")
        try:
            status = TaskStatus(data.get("status"))
        except (TypeError, ValueError) as error:
            raise TaskStoreValidationError("任务状态无效") from error
        task = cls(
            id=data.get("id"),
            album_id=data.get("album_id"),
            title=data.get("title"),
            status=status,
            progress=data.get("progress"),
            chapter=data.get("chapter"),
            page=data.get("page"),
            error=data.get("error"),
            pictures_directory=paths.get("pictures"),
            pdf_directory=paths.get("pdfs"),
            selected_chapter_ids=(
                None
                if legacy_schema
                else cls._decode_selected_chapter_ids(data)
            ),
        )
        task.validate()
        return task

    @classmethod
    def from_runtime(
        cls,
        task: Mapping,
        paths: AppPaths,
        portable_root: Path,
    ) -> "StoredTask":
        return cls(
            id=task["id"],
            album_id=task["album_id"],
            title=task.get("title"),
            status=TaskStatus(task["status"]),
            progress=int(task.get("progress", 0)),
            chapter=task.get("chapter", ""),
            page=task.get("page", ""),
            error=task.get("error"),
            pictures_directory=serialize_portable_path(
                portable_root,
                paths.pictures,
            ),
            pdf_directory=serialize_portable_path(
                portable_root,
                paths.pdfs,
            ),
            selected_chapter_ids=task.get("selected_chapter_ids"),
        )

    def _validate_selected_chapter_ids(self) -> None:
        values = self.selected_chapter_ids
        if values is None:
            return
        if not isinstance(values, tuple) or not values:
            raise TaskStoreValidationError("已选章节必须是非空数组")
        if len(values) != len(set(values)):
            raise TaskStoreValidationError("已选章节不能重复")
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 32
                or not value.isascii()
                or not value.isdigit()
            ):
                raise TaskStoreValidationError("已选章节编号无效")

    @staticmethod
    def _decode_selected_chapter_ids(
        data: Mapping,
    ) -> tuple[str, ...] | None:
        if "selected_chapter_ids" not in data:
            raise TaskStoreValidationError("任务记录缺少章节选择")
        values = data.get("selected_chapter_ids")
        if values is None:
            return None
        if not isinstance(values, list):
            raise TaskStoreValidationError("已选章节必须是数组")
        return tuple(values)

    @staticmethod
    def _validate_text(label: str, value: str) -> None:
        if not isinstance(value, str) or "\0" in value:
            raise TaskStoreValidationError(f"{label}无效")

    @classmethod
    def _validate_optional_text(cls, label: str, value: str | None) -> None:
        if value is not None:
            cls._validate_text(label, value)


class TaskStore:
    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        self.paths = paths
        self.last_recovery_backup: Path | None = None
        self.last_error: TaskStoreError | None = None
        self.needs_migration = False
        self._condition = threading.Condition()
        self._generation = 0
        self._completed_generation = 0
        self._pending: tuple[int, bytes] | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._closed = False

    def load(self) -> list[StoredTask]:
        self.last_recovery_backup = None
        self.needs_migration = False
        task_path = self.paths.tasks_file
        self._cleanup_stale_temporaries()
        if not task_path.is_file():
            return []
        try:
            raw = task_path.read_bytes()
        except OSError as error:
            raise TaskStoreError(f"无法读取任务记录：{error}") from error
        try:
            tasks, version = self._decode(raw)
            self.needs_migration = version < TASK_STORE_SCHEMA_VERSION
            return tasks
        except UnsupportedTaskStoreVersion:
            raise
        except (UnicodeError, json.JSONDecodeError, TaskStoreValidationError) as error:
            return self._recover_corrupt_store(raw, error)

    def save(self, tasks: Iterable[StoredTask]) -> int:
        payload = self._encode(tasks)
        with self._condition:
            if self._closing or self._closed:
                raise TaskStoreError("任务存储已经关闭")
            self._generation += 1
            generation = self._generation
            self._pending = (generation, payload)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._writer_loop,
                    name="task-store-writer",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return generation

    def flush(self, timeout: float | None = 5.0) -> bool:
        with self._condition:
            target = self._generation
            if target == 0:
                return True
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while self._completed_generation < target:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self.last_error is None

    def close(self, timeout: float | None = 5.0) -> bool:
        started = time.monotonic()
        flushed = self.flush(timeout)
        with self._condition:
            if self._closed:
                return flushed
            self._closing = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            remaining = None
            if timeout is not None:
                remaining = max(0.0, timeout - (time.monotonic() - started))
            thread.join(remaining)
        with self._condition:
            stopped = thread is None or not thread.is_alive()
            if stopped:
                self._closed = True
        return flushed and stopped

    def _writer_loop(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closing:
                    self._condition.wait()
                if self._pending is None and self._closing:
                    self._closed = True
                    self._condition.notify_all()
                    return
                generation, payload = self._pending
                self._pending = None
            error = None
            try:
                self._write_atomic(self.paths.tasks_file, payload)
            except TaskStoreError as caught:
                error = caught
            with self._condition:
                self._completed_generation = max(
                    self._completed_generation,
                    generation,
                )
                self.last_error = error
                self._condition.notify_all()

    def _recover_corrupt_store(
        self,
        raw: bytes,
        original_error: Exception,
    ) -> list[StoredTask]:
        backup_path = self._next_backup_path()
        try:
            self._write_atomic(backup_path, raw)
        except TaskStoreError as error:
            raise TaskStoreRecoveryError(
                f"任务记录已损坏，且无法创建备份：{error}"
            ) from original_error
        self.last_recovery_backup = backup_path
        try:
            self._write_atomic(self.paths.tasks_file, self._encode(()))
        except TaskStoreError as error:
            raise TaskStoreRecoveryError(
                f"任务记录已备份到 {backup_path}，但无法恢复空任务列表：{error}"
            ) from original_error
        return []

    def _next_backup_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.paths.tasks_file.with_name(
            f"{self.paths.tasks_file.name}.corrupt-{stamp}"
        )
        candidate = base
        suffix = 1
        while candidate.exists():
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        return candidate

    def _cleanup_stale_temporaries(self) -> None:
        pattern = f".{self.paths.tasks_file.name}.*.tmp"
        try:
            candidates = tuple(self.paths.tasks_file.parent.glob(pattern))
        except OSError:
            return
        for candidate in candidates:
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink()
            except OSError:
                continue

    @staticmethod
    def _decode(raw: bytes) -> tuple[list[StoredTask], int]:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, Mapping):
            raise TaskStoreValidationError("任务记录根节点必须是对象")
        version = data.get("schema_version")
        if type(version) is not int:
            raise TaskStoreValidationError("任务记录版本必须是整数")
        if version > TASK_STORE_SCHEMA_VERSION:
            raise UnsupportedTaskStoreVersion(
                f"任务记录版本 {version} 高于程序支持的版本 "
                f"{TASK_STORE_SCHEMA_VERSION}"
            )
        if version not in {1, TASK_STORE_SCHEMA_VERSION}:
            raise TaskStoreValidationError("不支持的任务记录版本")
        values = data.get("tasks")
        if not isinstance(values, list):
            raise TaskStoreValidationError("任务列表必须是数组")
        tasks = [
            StoredTask.from_dict(value, legacy_schema=version == 1)
            for value in values
        ]
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise TaskStoreValidationError("任务 ID 不能重复")
        return tasks, version

    @staticmethod
    def _encode(tasks: Iterable[StoredTask]) -> bytes:
        values = list(tasks)
        ids = []
        encoded = []
        for task in values:
            if not isinstance(task, StoredTask):
                raise TaskStoreValidationError("任务存储只接受 StoredTask")
            task.validate()
            ids.append(task.id)
            encoded.append(task.to_dict())
        if len(ids) != len(set(ids)):
            raise TaskStoreValidationError("任务 ID 不能重复")
        return (
            json.dumps(
                {
                    "schema_version": TASK_STORE_SCHEMA_VERSION,
                    "tasks": encoded,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _write_atomic(target: Path, payload: bytes) -> None:
        temporary = None
        descriptor = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        except OSError as error:
            raise TaskStoreError(f"无法保存任务记录：{error}") from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
