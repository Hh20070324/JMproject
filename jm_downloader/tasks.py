import queue
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from .downloader import DownloadWorker
from .settings import AppPaths, DEFAULT_PATHS


class TaskError(Exception):
    pass


class TaskNotFound(TaskError):
    pass


class TaskConflict(TaskError):
    pass


class InvalidTaskState(TaskError):
    pass


class TaskManager:
    ACTIVE_STATUSES = ("fetching", "downloading")

    def __init__(
        self,
        paths: AppPaths = DEFAULT_PATHS,
        max_concurrent: int = 2,
        worker_factory: Callable = DownloadWorker,
    ):
        self.paths = paths
        self.max_concurrent = max_concurrent
        self.worker_factory = worker_factory
        self._lock = threading.Lock()
        self._tasks = []
        self._workers = {}
        self._listeners_lock = threading.Lock()
        self._listeners = []

    def list_tasks(self) -> list[dict]:
        with self._lock:
            tasks = []
            for task in self._tasks:
                public_task = {
                    key: value for key, value in task.items() if key != "_preview_path"
                }
                if task.get("_preview_path"):
                    version = task.get("preview_version", 0)
                    public_task["preview"] = (
                        f"/api/tasks/{task['id']}/preview?v={version}"
                    )
                tasks.append(public_task)
            return tasks

    def add(self, album_id: str) -> dict:
        with self._lock:
            if any(
                task["album_id"] == album_id
                and task["status"] not in ("completed", "failed")
                for task in self._tasks
            ):
                raise TaskConflict("该车号已在队列中")

            task = {
                "id": str(uuid.uuid4())[:8],
                "album_id": album_id,
                "title": None,
                "cover": None,
                "preview": None,
                "preview_version": 0,
                "_preview_path": None,
                "status": "pending",
                "progress": 0,
                "chapter": "",
                "error": None,
                "pdf": None,
            }
            self._tasks.append(task)

        self.broadcast({"type": "added", "id": task["id"], "album_id": album_id})
        self.schedule()
        return dict(task)

    def is_active(self, album_id: str) -> bool:
        with self._lock:
            return any(
                task["album_id"] == album_id
                and task["status"] in ("pending", *self.ACTIVE_STATUSES)
                for task in self._tasks
            )

    def has_active_tasks(self) -> bool:
        with self._lock:
            return any(
                task["status"] in ("pending", *self.ACTIVE_STATUSES)
                for task in self._tasks
            )

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()

    def remove(self, task_id: str) -> None:
        worker = None
        with self._lock:
            for index, task in enumerate(self._tasks):
                if task["id"] != task_id:
                    continue
                worker = self._workers.pop(task_id, None)
                del self._tasks[index]
                break
            else:
                raise TaskNotFound("未找到该任务")

        if worker:
            worker.stop()
        self.broadcast({"type": "removed", "id": task_id})
        self.schedule()

    def retry(self, task_id: str) -> None:
        with self._lock:
            task = self._find_locked(task_id)
            if task["status"] != "failed":
                raise InvalidTaskState("任务不存在或不可重试")
            task.update(status="pending", error=None, progress=0)

        self.broadcast({"type": "retry", "id": task_id})
        self.schedule()

    def schedule(self) -> None:
        while True:
            with self._lock:
                active_count = sum(
                    task["status"] in self.ACTIVE_STATUSES for task in self._tasks
                )
                if active_count >= self.max_concurrent:
                    return

                task = next(
                    (task for task in self._tasks if task["status"] == "pending"),
                    None,
                )
                if task is None:
                    return
                task["status"] = "fetching"
                task_id = task["id"]
                album_id = task["album_id"]

            worker = self.worker_factory(
                album_id,
                on_info=lambda aid, title, cover, tid=task_id: self._on_info(
                    tid, title, cover
                ),
                on_progress=lambda aid, pct, chapter, page, tid=task_id: self._on_progress(
                    tid, pct, chapter, page
                ),
                on_complete=lambda aid, pdf, tid=task_id: self._on_complete(tid, pdf),
                on_error=lambda aid, error, tid=task_id: self._on_error(tid, error),
                on_preview=lambda aid, path, tid=task_id: self._on_preview(tid, path),
                paths=self.paths,
            )
            with self._lock:
                self._workers[task_id] = worker

            try:
                worker.start()
            except Exception as error:
                self._on_error(task_id, f"启动失败: {error}")

    def add_listener(self) -> queue.Queue:
        listener = queue.Queue(maxsize=200)
        with self._listeners_lock:
            self._listeners.append(listener)
        return listener

    def get_preview_path(self, task_id: str):
        with self._lock:
            preview = self._find_locked(task_id).get("_preview_path")
        if not preview:
            raise TaskNotFound("预览图尚未生成")

        preview_path = Path(preview).resolve()
        pictures_path = self.paths.pictures.resolve()
        if not preview_path.is_relative_to(pictures_path) or not preview_path.is_file():
            raise TaskNotFound("预览图不存在")
        return preview_path

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

    def _update(self, task_id: str, **changes) -> bool:
        with self._lock:
            try:
                self._find_locked(task_id).update(changes)
                return True
            except TaskNotFound:
                return False

    def _on_info(self, task_id: str, title: str, cover: str) -> None:
        tasks = self.list_tasks()
        task = next((item for item in tasks if item["id"] == task_id), None)
        if task is None:
            return
        resolved_title = title or f"#{task['album_id']}"
        self._update(task_id, title=resolved_title, cover=cover, status="downloading")
        self.broadcast(
            {"type": "info", "id": task_id, "title": resolved_title, "cover": cover}
        )

    def _on_progress(
        self, task_id: str, percent: int, chapter: str, page: str
    ) -> None:
        if not self._update(
            task_id, progress=percent, chapter=chapter, status="downloading"
        ):
            return
        self.broadcast(
            {
                "type": "progress",
                "id": task_id,
                "percent": percent,
                "chapter": chapter,
                "page": page,
            }
        )

    def _on_complete(self, task_id: str, pdf_path: str) -> None:
        if not self._update(
            task_id, status="completed", progress=100, pdf=pdf_path
        ):
            return
        with self._lock:
            self._workers.pop(task_id, None)
        self.broadcast({"type": "completed", "id": task_id, "pdf": pdf_path})
        self.schedule()

    def _on_preview(self, task_id: str, preview_path: str) -> None:
        path = Path(preview_path)
        if not path.is_absolute():
            path = self.paths.root / path
        with self._lock:
            try:
                task = self._find_locked(task_id)
            except TaskNotFound:
                return
            version = int(task.get("preview_version", 0)) + 1
            task.update(_preview_path=str(path), preview_version=version)
        self.broadcast(
            {
                "type": "preview",
                "id": task_id,
                "preview": f"/api/tasks/{task_id}/preview?v={version}",
            }
        )

    def _on_error(self, task_id: str, error: str) -> None:
        if not self._update(task_id, status="failed", error=error):
            return
        with self._lock:
            self._workers.pop(task_id, None)
        self.broadcast({"type": "failed", "id": task_id, "error": error})
        self.schedule()
