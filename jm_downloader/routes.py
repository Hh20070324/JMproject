import json
import queue

from flask import Blueprint, Response, jsonify, request, send_file

from .library import LibraryError, LibraryNotFound, LibraryService
from .models import LibraryItem, TaskSnapshot
from .tasks import (
    InvalidAlbumId,
    InvalidTaskState,
    TaskConflict,
    TaskManager,
    TaskNotFound,
)


def task_payload(task: TaskSnapshot) -> dict:
    preview = None
    if task.preview_path is not None:
        preview = f"/api/tasks/{task.id}/preview?v={task.preview_revision}"
    pdf = None
    if task.pdf_path is not None:
        pdf = f"/api/library/{task.album_id}/pdf"
    return {
        "id": task.id,
        "album_id": task.album_id,
        "title": task.title,
        "cover": task.cover_url,
        "preview": preview,
        "status": task.status.value,
        "progress": task.progress,
        "chapter": task.chapter,
        "page": task.page,
        "error": task.error,
        "pdf": pdf,
    }


def task_event_payload(event: dict) -> dict:
    payload = {
        key: value
        for key, value in event.items()
        if key not in ("preview_path", "preview_revision", "pdf_path")
    }
    if event.get("type") == "preview":
        payload["preview"] = (
            f"/api/tasks/{event['id']}/preview?v={event['preview_revision']}"
        )
    elif event.get("type") == "completed":
        payload["pdf"] = f"/api/library/{event['album_id']}/pdf"
    return payload


def library_payload(item: LibraryItem) -> dict:
    return {
        "album_id": item.album_id,
        "chapter_count": item.chapter_count,
        "image_count": item.image_count,
        "image_size": item.image_size,
        "has_images": item.has_images,
        "has_pdf": item.has_pdf,
        "pdf_size": item.pdf_size,
        "preview": (
            f"/api/library/{item.album_id}/preview" if item.has_images else None
        ),
        "pdf": f"/api/library/{item.album_id}/pdf" if item.has_pdf else None,
    }


def create_api_blueprint(
    manager: TaskManager, library: LibraryService
) -> Blueprint:
    api = Blueprint("api", __name__, url_prefix="/api")

    @api.post("/add")
    def add_task():
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"error": "无效的请求数据"}), 400

        try:
            task = manager.add(data.get("album_id", ""))
        except InvalidAlbumId as error:
            return jsonify({"error": str(error)}), 400
        except TaskConflict as error:
            return jsonify({"error": str(error)}), 409
        except InvalidTaskState as error:
            return jsonify({"error": str(error)}), 409
        return jsonify({"id": task.id, "album_id": task.album_id})

    @api.get("/queue")
    def queue_state():
        return jsonify([task_payload(task) for task in manager.list_tasks()])

    @api.delete("/remove/<task_id>")
    def remove_task(task_id):
        try:
            manager.remove(task_id)
        except TaskNotFound as error:
            return jsonify({"error": str(error)}), 404
        except InvalidTaskState as error:
            return jsonify({"error": str(error)}), 409
        return jsonify({"ok": True})

    @api.post("/retry/<task_id>")
    def retry_task(task_id):
        try:
            manager.retry(task_id)
        except (TaskNotFound, InvalidTaskState) as error:
            return jsonify({"error": str(error)}), 400
        return jsonify({"ok": True})

    @api.get("/events")
    def events():
        def event_stream():
            listener = manager.add_listener()
            initial = json.dumps(
                [task_payload(task) for task in manager.list_tasks()],
                ensure_ascii=False,
            )
            yield f"event: init\ndata: {initial}\n\n"
            try:
                while True:
                    try:
                        event = listener.get(timeout=30)
                        payload = json.dumps(
                            task_event_payload(event), ensure_ascii=False
                        )
                        yield f"data: {payload}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                manager.remove_listener(listener)

        return Response(
            event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @api.get("/tasks/<task_id>/preview")
    def task_preview(task_id):
        try:
            preview_path = manager.get_preview_path(task_id)
        except TaskNotFound as error:
            return jsonify({"error": str(error)}), 404
        return send_file(preview_path, conditional=True, max_age=0)

    @api.get("/library")
    def library_items():
        return jsonify([library_payload(item) for item in library.list_items()])

    @api.get("/library/<album_id>/preview")
    def library_preview(album_id):
        try:
            return send_file(library.get_preview(album_id), conditional=True, max_age=0)
        except LibraryNotFound as error:
            return jsonify({"error": str(error)}), 404

    @api.get("/library/<album_id>/pdf")
    def library_pdf(album_id):
        try:
            return send_file(library.get_pdf(album_id), conditional=True)
        except LibraryNotFound as error:
            return jsonify({"error": str(error)}), 404

    @api.post("/library/<album_id>/pdf")
    def rebuild_library_pdf(album_id):
        if manager.is_active(album_id):
            return jsonify({"error": "下载进行中，暂时不能生成 PDF"}), 409
        try:
            library.rebuild_pdf(album_id)
            return jsonify(library_payload(library.get_item(album_id)))
        except LibraryNotFound as error:
            return jsonify({"error": str(error)}), 404
        except LibraryError as error:
            return jsonify({"error": str(error)}), 400

    @api.delete("/library/<album_id>/<kind>")
    def delete_library_files(album_id, kind):
        if manager.is_active(album_id):
            return jsonify({"error": "下载进行中，暂时不能删除文件"}), 409
        try:
            if kind == "images":
                library.delete_images(album_id)
            elif kind == "pdf":
                library.delete_pdf(album_id)
            else:
                return jsonify({"error": "不支持的删除类型"}), 400
        except LibraryNotFound as error:
            return jsonify({"error": str(error)}), 404
        return jsonify({"ok": True})

    @api.post("/library/<album_id>/open/<kind>")
    def open_library_file(album_id, kind):
        try:
            library.open_location(album_id, kind)
        except LibraryNotFound as error:
            return jsonify({"error": str(error)}), 404
        except LibraryError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify({"ok": True})

    return api
