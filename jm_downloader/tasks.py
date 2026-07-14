import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .downloader import DownloadWorker
from .models import TaskSnapshot, TaskStatus
from .settings import AppPaths, DEFAULT_PATHS
from .task_store import StoredTask, TaskStore, TaskStoreError


class TaskError(Exception):
    pass


class TaskNotFound(TaskError):
    pass


class TaskConflict(TaskError):
    pass


class InvalidTaskState(TaskError):
    pass


class InvalidAlbumId(TaskError):
    pass


def normalize_album_id(value: str) -> str:
    album_id = str(value).strip()
    if album_id[:2].lower() == "jm":
        album_id = album_id[2:].strip()
    if not album_id:
        raise InvalidAlbumId("车号不能为空")
    if not album_id.isascii() or not album_id.isdigit():
        raise InvalidAlbumId("车号只能包含数字")
    return album_id


@dataclass(slots=True)
class _WorkerHandle:
    generation: int
    worker: object


class TaskManager:
    ACTIVE_STATUSES = (TaskStatus.FETCHING.value, TaskStatus.DOWNLOADING.value)
    WORKER_STATUSES = (
        *ACTIVE_STATUSES,
        TaskStatus.PAUSING.value,
        TaskStatus.CANCELLING.value,
    )
    RESERVED_STATUSES = (
        TaskStatus.PENDING.value,
        *WORKER_STATUSES,
        TaskStatus.PAUSED.value,
        TaskStatus.FAILED.value,
    )
    SHUTDOWN_PAUSE_STATUSES = (
        TaskStatus.PENDING.value,
        *WORKER_STATUSES,
    )

    def __init__(
        self,
        paths: AppPaths = DEFAULT_PATHS,
        max_concurrent: int = 2,
        worker_factory: Callable = DownloadWorker,
        task_store: TaskStore | None = None,
    ):
        self.paths = paths
        self.max_concurrent = max_concurrent
        self.worker_factory = worker_factory
        self.task_store = task_store or TaskStore(paths)
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._workers = {}
        self._listeners_lock = threading.Lock()
        self._listeners = []
        self._stopping = False
        self._library_operations = set()
        stored_tasks = self.task_store.load()
        self._persistence_started = bool(stored_tasks) or self.paths.tasks_file.exists()
        self._tasks = [self._restore_task(task) for task in stored_tasks]
        if any(
            task.status not in (TaskStatus.PAUSED, TaskStatus.FAILED)
            for task in stored_tasks
        ):
            self._queue_persist()

    def list_tasks(self) -> list[TaskSnapshot]:
        with self._lock:
            return [self._snapshot_locked(task) for task in self._tasks]

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            return self._snapshot_locked(self._find_locked(task_id))

    def add(self, album_id: str) -> TaskSnapshot:
        album_id = normalize_album_id(album_id)
        with self._lock:
            if self._stopping:
                raise InvalidTaskState("任务管理器正在关闭")
            if album_id in self._library_operations:
                raise TaskConflict("该漫画正在进行本地库操作")
            if any(
                task["album_id"] == album_id
                and task["status"] != TaskStatus.COMPLETED.value
                for task in self._tasks
            ):
                raise TaskConflict("该车号已在队列中")

            task = {
                "id": str(uuid.uuid4())[:8],
                "album_id": album_id,
                "title": None,
                "cover_url": None,
                "preview_revision": 0,
                "_preview_path": None,
                "status": TaskStatus.PENDING.value,
                "progress": 0,
                "chapter": "",
                "page": "",
                "error": None,
                "pdf_path": None,
                "run_generation": 0,
                "_paths": self.paths,
            }
            self._tasks.append(task)
            created = self._snapshot_locked(task)

        self._queue_persist()
        self.broadcast({"type": "added", "id": task["id"], "album_id": album_id})
        self.schedule()
        return created

    def is_active(self, album_id: str) -> bool:
        with self._lock:
            return any(
                task["album_id"] == album_id
                and task["status"] in self.RESERVED_STATUSES
                for task in self._tasks
            )

    def begin_library_operation(self, album_id: str) -> str:
        album_id = normalize_album_id(album_id)
        with self._lock:
            if self._stopping:
                raise InvalidTaskState("任务管理器正在关闭")
            if album_id in self._library_operations:
                raise TaskConflict("该漫画正在进行本地库操作")
            if any(
                task["album_id"] == album_id
                and task["status"] in self.RESERVED_STATUSES
                for task in self._tasks
            ):
                raise TaskConflict("该漫画仍有下载任务，暂不可修改")
            self._library_operations.add(album_id)
        return album_id

    def end_library_operation(self, album_id: str) -> None:
        album_id = normalize_album_id(album_id)
        with self._lock:
            self._library_operations.discard(album_id)

    def is_library_operation_active(self, album_id: str) -> bool:
        try:
            album_id = normalize_album_id(album_id)
        except InvalidAlbumId:
            return False
        with self._lock:
            return album_id in self._library_operations

    def has_active_tasks(self) -> bool:
        with self._lock:
            return any(
                task["status"]
                in (TaskStatus.PENDING.value, *self.WORKER_STATUSES)
                for task in self._tasks
            )

    def stop_all(self) -> None:
        with self._lock:
            handles = []
            changed = []
            for task in self._tasks:
                status = task["status"]
                if status == TaskStatus.PENDING.value:
                    task["status"] = TaskStatus.PAUSED.value
                    changed.append((task["id"], TaskStatus.PAUSED.value))
                    continue
                if status not in self.ACTIVE_STATUSES:
                    continue
                handle = self._workers.get(task["id"])
                if handle is None:
                    task["status"] = TaskStatus.PAUSED.value
                    changed.append((task["id"], TaskStatus.PAUSED.value))
                    continue
                task["status"] = TaskStatus.PAUSING.value
                handles.append(handle)
                changed.append((task["id"], TaskStatus.PAUSING.value))
        if changed:
            self._queue_persist()
        for task_id, status in changed:
            self.broadcast({"type": status, "id": task_id})
        for handle in handles:
            handle.worker.stop()
        self.schedule()

    def shutdown(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            with self._lock:
                self._stopping = True
                for task in self._tasks:
                    if task["status"] in self.SHUTDOWN_PAUSE_STATUSES:
                        task["status"] = TaskStatus.PAUSED.value
                handles = list(self._workers.items())

            self._queue_persist()
            for _, handle in handles:
                handle.worker.stop()

        deadline = time.monotonic() + max(0.0, timeout)
        all_finished = True
        finished = []
        store_finished = False
        try:
            for task_id, handle in handles:
                wait = getattr(handle.worker, "wait", None)
                if wait is None:
                    all_finished = False
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                worker_finished = bool(wait(remaining))
                all_finished = worker_finished and all_finished
                if worker_finished:
                    finished.append((task_id, handle.generation))
        finally:
            with self._lock:
                for task_id, generation in finished:
                    self._retire_worker_locked(task_id, generation)
            remaining = max(1.0, deadline - time.monotonic())
            store_finished = self.task_store.close(remaining)
        return all_finished and store_finished

    def remove(self, task_id: str) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._stopping:
                    raise InvalidTaskState("任务管理器正在关闭")
                for index, task in enumerate(self._tasks):
                    if task["id"] != task_id:
                        continue
                    if task["status"] in self.WORKER_STATUSES:
                        raise InvalidTaskState("下载中的任务暂不支持移除")
                    handle = self._workers.pop(task_id, None)
                    del self._tasks[index]
                    break
                else:
                    raise TaskNotFound("未找到该任务")

            if handle is not None:
                handle.worker.stop()
        self._queue_persist()
        self.broadcast({"type": "removed", "id": task_id})
        self.schedule()

    def pause(self, task_id: str) -> None:
        handle = None
        with self._lifecycle_lock:
            with self._lock:
                if self._stopping:
                    raise InvalidTaskState("任务管理器正在关闭")
                task = self._find_locked(task_id)
                status = task["status"]
                if status == TaskStatus.PENDING.value:
                    task["status"] = TaskStatus.PAUSED.value
                    event_type = "paused"
                elif status in self.ACTIVE_STATUSES:
                    handle = self._workers.get(task_id)
                    if handle is None:
                        task["status"] = TaskStatus.PAUSED.value
                        event_type = "paused"
                    else:
                        task["status"] = TaskStatus.PAUSING.value
                        event_type = "pausing"
                else:
                    raise InvalidTaskState("任务当前不可暂停")

            self._queue_persist()
            self.broadcast({"type": event_type, "id": task_id})
            if handle is not None:
                handle.worker.stop()
        self.schedule()

    def resume(self, task_id: str) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._stopping:
                    raise InvalidTaskState("任务管理器正在关闭")
                task = self._find_locked(task_id)
                if task["status"] not in (
                    TaskStatus.PAUSED.value,
                    TaskStatus.FAILED.value,
                ):
                    raise InvalidTaskState("任务当前不可继续")
                if task["album_id"] in self._library_operations:
                    raise TaskConflict("该漫画正在进行本地库操作")
                task.update(
                    status=TaskStatus.PENDING.value,
                    error=None,
                )

            self._queue_persist()
            self.broadcast({"type": "resumed", "id": task_id})
        self.schedule()

    def retry(self, task_id: str) -> None:
        self.resume(task_id)

    def cancel(self, task_id: str) -> None:
        handle = None
        removed = False
        with self._lifecycle_lock:
            with self._lock:
                if self._stopping:
                    raise InvalidTaskState("任务管理器正在关闭")
                task = self._find_locked(task_id)
                status = task["status"]
                if status in (
                    TaskStatus.PENDING.value,
                    TaskStatus.PAUSED.value,
                    TaskStatus.FAILED.value,
                ):
                    self._tasks.remove(task)
                    self._workers.pop(task_id, None)
                    removed = True
                elif status in self.ACTIVE_STATUSES:
                    handle = self._workers.get(task_id)
                    if handle is None:
                        self._tasks.remove(task)
                        removed = True
                    else:
                        task["status"] = TaskStatus.CANCELLING.value
                else:
                    raise InvalidTaskState("任务当前不可取消")

            self._queue_persist()
            self.broadcast(
                {
                    "type": "cancelled" if removed else "cancelling",
                    "id": task_id,
                }
            )
            if handle is not None:
                handle.worker.stop()
        if removed:
            self.schedule()

    def schedule(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                active_count = sum(
                    task["status"] in self.WORKER_STATUSES for task in self._tasks
                )
                if active_count >= self.max_concurrent:
                    return

                task = next(
                    (
                        task
                        for task in self._tasks
                        if task["status"] == TaskStatus.PENDING.value
                    ),
                    None,
                )
                if task is None:
                    return
                task["status"] = TaskStatus.FETCHING.value
                task_id = task["id"]
                album_id = task["album_id"]
                task_paths = task["_paths"]
                task["run_generation"] += 1
                generation = task["run_generation"]

            self._queue_persist()
            try:
                callbacks = self._worker_callbacks(task_id, generation)
                worker = self.worker_factory(
                    album_id,
                    paths=task_paths,
                    **callbacks,
                )
            except Exception as error:
                self._on_error(task_id, generation, f"创建下载任务失败: {error}")
                return

            start_error = None
            should_start = False
            with self._lifecycle_lock:
                with self._lock:
                    task = next(
                        (item for item in self._tasks if item["id"] == task_id),
                        None,
                    )
                    if (
                        not self._stopping
                        and task is not None
                        and task["run_generation"] == generation
                        and task["status"] == TaskStatus.FETCHING.value
                    ):
                        self._workers[task_id] = _WorkerHandle(generation, worker)
                        should_start = True
                if should_start:
                    try:
                        worker.start()
                    except Exception as error:
                        start_error = error
            if not should_start:
                worker.stop()
                return
            if start_error is not None:
                self._on_error(task_id, generation, f"启动失败: {start_error}")

    def add_listener(self) -> queue.Queue:
        listener = queue.Queue(maxsize=200)
        with self._listeners_lock:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: queue.Queue) -> None:
        with self._listeners_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def broadcast(self, event: dict) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

    def _find_locked(self, task_id: str) -> dict:
        task = next((task for task in self._tasks if task["id"] == task_id), None)
        if task is None:
            raise TaskNotFound("未找到该任务")
        return task

    @staticmethod
    def _snapshot_locked(task: dict) -> TaskSnapshot:
        preview = task.get("_preview_path")
        pdf = task.get("pdf_path")
        return TaskSnapshot(
            id=task["id"],
            album_id=task["album_id"],
            title=task.get("title"),
            status=TaskStatus(task["status"]),
            progress=int(task.get("progress", 0)),
            chapter=task.get("chapter", ""),
            page=task.get("page", ""),
            preview_path=Path(preview) if preview else None,
            preview_revision=int(task.get("preview_revision", 0)),
            pdf_path=Path(pdf) if pdf else None,
            error=task.get("error"),
            cover_url=task.get("cover_url"),
        )

    def _restore_task(self, stored: StoredTask) -> dict:
        status = (
            TaskStatus.FAILED
            if stored.status == TaskStatus.FAILED
            else TaskStatus.PAUSED
        )
        return {
            "id": stored.id,
            "album_id": stored.album_id,
            "title": stored.title,
            "cover_url": None,
            "preview_revision": 0,
            "_preview_path": None,
            "status": status.value,
            "progress": stored.progress,
            "chapter": stored.chapter,
            "page": stored.page,
            "error": stored.error if status == TaskStatus.FAILED else None,
            "pdf_path": None,
            "run_generation": 0,
            "_paths": stored.to_paths(self.paths.root),
        }

    def _queue_persist(self) -> None:
        with self._lock:
            records = tuple(
                StoredTask.from_runtime(
                    task,
                    task["_paths"],
                    self.paths.root,
                )
                for task in self._tasks
                if task["status"] != TaskStatus.COMPLETED.value
            )
        if not records and not self._persistence_started:
            return
        try:
            self.task_store.save(records)
            self._persistence_started = True
        except TaskStoreError:
            if not self._stopping:
                raise

    def _find_active_generation_locked(
        self, task_id: str, generation: int
    ) -> dict | None:
        try:
            task = self._find_locked(task_id)
        except TaskNotFound:
            return None
        if task["run_generation"] != generation:
            return None
        if task["status"] not in self.ACTIVE_STATUSES:
            return None
        return task

    def _worker_callbacks(self, task_id: str, generation: int) -> dict:
        def on_info(_album_id, title, cover):
            self._on_info(task_id, generation, title, cover)

        def on_progress(_album_id, percent, chapter, page):
            self._on_progress(task_id, generation, percent, chapter, page)

        def on_complete(_album_id, pdf_path):
            self._on_complete(task_id, generation, pdf_path)

        def on_error(_album_id, error):
            self._on_error(task_id, generation, error)

        def on_preview(_album_id, preview_path):
            self._on_preview(task_id, generation, preview_path)

        def on_stopped(_album_id):
            self._on_stopped(task_id, generation)

        return {
            "on_info": on_info,
            "on_progress": on_progress,
            "on_complete": on_complete,
            "on_error": on_error,
            "on_preview": on_preview,
            "on_stopped": on_stopped,
        }

    def _retire_worker_locked(self, task_id: str, generation: int) -> None:
        handle = self._workers.get(task_id)
        if handle is not None and handle.generation == generation:
            self._workers.pop(task_id, None)

    def _on_info(
        self, task_id: str, generation: int, title: str, cover: str
    ) -> None:
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            resolved_title = title or f"#{task['album_id']}"
            task.update(
                title=resolved_title,
                cover_url=cover,
                status=TaskStatus.DOWNLOADING.value,
            )
        self.broadcast(
            {"type": "info", "id": task_id, "title": resolved_title, "cover": cover}
        )
        self._queue_persist()

    def _on_progress(
        self,
        task_id: str,
        generation: int,
        percent: int,
        chapter: str,
        page: str,
    ) -> None:
        percent = max(0, min(100, int(percent)))
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            task.update(
                progress=percent,
                chapter=chapter,
                page=page,
                status=TaskStatus.DOWNLOADING.value,
            )
        self.broadcast(
            {
                "type": "progress",
                "id": task_id,
                "percent": percent,
                "chapter": chapter,
                "page": page,
            }
        )
        self._queue_persist()

    def _on_complete(
        self, task_id: str, generation: int, pdf_path: str
    ) -> None:
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            task_paths = task["_paths"]
        path = Path(pdf_path)
        if not path.is_absolute():
            path = task_paths.root / path
        path = path.resolve()
        if not path.is_relative_to(task_paths.pdfs.resolve()):
            self._on_error(
                task_id, generation, "PDF 输出路径不在受管目录中"
            )
            return
        if not path.is_file():
            self._on_error(task_id, generation, "PDF 文件不存在")
            return
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            task.update(
                status=TaskStatus.COMPLETED.value,
                progress=100,
                pdf_path=str(path),
            )
            album_id = task["album_id"]
            self._retire_worker_locked(task_id, generation)
        self.broadcast(
            {
                "type": "completed",
                "id": task_id,
                "album_id": album_id,
                "pdf_path": path,
            }
        )
        self._queue_persist()
        self.schedule()

    def _on_preview(
        self, task_id: str, generation: int, preview_path: str
    ) -> None:
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            task_paths = task["_paths"]
        path = Path(preview_path)
        if not path.is_absolute():
            path = task_paths.root / path
        path = path.resolve()
        if not path.is_relative_to(task_paths.pictures.resolve()) or not path.is_file():
            return
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            revision = int(task.get("preview_revision", 0)) + 1
            task.update(_preview_path=str(path), preview_revision=revision)
        self.broadcast(
            {
                "type": "preview",
                "id": task_id,
                "preview_path": path,
                "preview_revision": revision,
            }
        )
        self._queue_persist()

    def _on_error(self, task_id: str, generation: int, error: str) -> None:
        with self._lock:
            task = self._find_active_generation_locked(task_id, generation)
            if task is None:
                return
            task.update(status=TaskStatus.FAILED.value, error=error)
            self._retire_worker_locked(task_id, generation)
        self.broadcast({"type": "failed", "id": task_id, "error": error})
        self._queue_persist()
        self.schedule()

    def _on_stopped(self, task_id: str, generation: int) -> None:
        event = None
        with self._lock:
            try:
                task = self._find_locked(task_id)
            except TaskNotFound:
                return
            if task["run_generation"] != generation:
                return

            status = task["status"]
            self._retire_worker_locked(task_id, generation)
            if status == TaskStatus.PAUSING.value:
                task.update(status=TaskStatus.PAUSED.value, error=None)
                event = {"type": "paused", "id": task_id}
            elif status == TaskStatus.CANCELLING.value:
                self._tasks.remove(task)
                event = {"type": "cancelled", "id": task_id}
            elif status in self.ACTIVE_STATUSES:
                message = "下载任务意外停止，请点击继续重试"
                task.update(status=TaskStatus.FAILED.value, error=message)
                event = {"type": "failed", "id": task_id, "error": message}

        if event is None:
            return
        self._queue_persist()
        self.broadcast(event)
        self.schedule()
