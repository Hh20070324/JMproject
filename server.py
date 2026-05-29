import os
import sys
import json
import uuid
import queue
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
os.chdir(str(PROJECT_ROOT))

from flask import Flask, request, jsonify, Response, send_from_directory
from download_worker import DownloadWorker

app = Flask(__name__, static_folder="static", static_url_path="")

# ---- Queue State ----
_task_lock = threading.Lock()
_tasks: list[dict] = []
_events: list[dict] = []
_active_workers: dict[str, DownloadWorker] = {}
MAX_CONCURRENT = 2

# ---- SSE Helpers ----
_sse_listeners: list[queue.Queue] = []


def broadcast_event(event: dict):
    global _events
    _events.append(event)
    if len(_events) > 500:
        _events = _events[-500:]
    for q in _sse_listeners:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def _schedule_next():
    active_count = len([t for t in _tasks if t["status"] in ("fetching", "downloading")])
    if active_count >= MAX_CONCURRENT:
        return

    with _task_lock:
        for t in _tasks:
            if t["status"] == "pending":
                t["status"] = "fetching"
                task_id = t["id"]
                album_id = t["album_id"]
                break
        else:
            return

    worker = DownloadWorker(
        album_id,
        on_info=lambda aid, title, cover: _on_info(task_id, title, cover),
        on_progress=lambda aid, pct, ch, pg: _on_progress(task_id, pct, ch, pg),
        on_complete=lambda aid, pdf_path: _on_complete(task_id, pdf_path),
        on_error=lambda aid, err: _on_error(task_id, err),
    )

    with _task_lock:
        _active_workers[task_id] = worker
        for t in _tasks:
            if t["id"] == task_id:
                t["status"] = "fetching"
                break

    worker.start()


def _on_info(task_id: str, title: str, cover: str):
    with _task_lock:
        for t in _tasks:
            if t["id"] == task_id:
                t["title"] = title or f"#{t['album_id']}"
                t["cover"] = cover
                t["status"] = "downloading"
                break
    broadcast_event({"type": "info", "id": task_id, "title": title, "cover": cover})


def _on_progress(task_id: str, pct: int, chapter: str, page: str):
    with _task_lock:
        for t in _tasks:
            if t["id"] == task_id:
                t["progress"] = pct
                t["chapter"] = chapter
                t["status"] = "downloading"
                break
    broadcast_event(
        {
            "type": "progress",
            "id": task_id,
            "percent": pct,
            "chapter": chapter,
            "page": page,
        }
    )


def _on_complete(task_id: str, pdf_path: str):
    with _task_lock:
        for t in _tasks:
            if t["id"] == task_id:
                t["status"] = "completed"
                t["progress"] = 100
                t["pdf"] = pdf_path
                break
        if task_id in _active_workers:
            del _active_workers[task_id]
    broadcast_event({"type": "completed", "id": task_id, "pdf": pdf_path})
    _schedule_next()


def _on_error(task_id: str, error: str):
    with _task_lock:
        for t in _tasks:
            if t["id"] == task_id:
                t["status"] = "failed"
                t["error"] = error
                break
        if task_id in _active_workers:
            del _active_workers[task_id]
    broadcast_event({"type": "failed", "id": task_id, "error": error})
    _schedule_next()


# ---- API Routes ----

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/add", methods=["POST"])
def api_add():
    data = request.get_json(force=True)
    album_id = str(data.get("album_id", "")).strip()
    if not album_id:
        return jsonify({"error": "车号不能为空"}), 400

    with _task_lock:
        for t in _tasks:
            if t["album_id"] == album_id and t["status"] not in ("completed", "failed"):
                return jsonify({"error": "该车号已在队列中"}), 409

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "album_id": album_id,
        "title": None,
        "cover": None,
        "status": "pending",
        "progress": 0,
        "chapter": "",
        "error": None,
        "pdf": None,
    }

    with _task_lock:
        _tasks.append(task)

    broadcast_event({"type": "added", "id": task_id, "album_id": album_id})

    _schedule_next()

    return jsonify({"id": task_id, "album_id": album_id})


@app.route("/api/queue", methods=["GET"])
def api_queue():
    with _task_lock:
        return jsonify(list(_tasks))


@app.route("/api/remove/<task_id>", methods=["DELETE"])
def api_remove(task_id):
    with _task_lock:
        for i, t in enumerate(_tasks):
            if t["id"] == task_id:
                if t["status"] in ("downloading", "fetching"):
                    worker = _active_workers.pop(task_id, None)
                    if worker:
                        worker.stop()
                del _tasks[i]
                broadcast_event({"type": "removed", "id": task_id})
                return jsonify({"ok": True})
    return jsonify({"error": "未找到该任务"}), 404


@app.route("/api/retry/<task_id>", methods=["POST"])
def api_retry(task_id):
    with _task_lock:
        for t in _tasks:
            if t["id"] == task_id and t["status"] == "failed":
                t["status"] = "pending"
                t["error"] = None
                t["progress"] = 0
                broadcast_event({"type": "retry", "id": task_id})
                break
        else:
            return jsonify({"error": "任务不存在或不可重试"}), 400
    _schedule_next()
    return jsonify({"ok": True})


@app.route("/api/events")
def api_events():
    def event_stream():
        q: queue.Queue = queue.Queue(maxsize=200)
        _sse_listeners.append(q)

        try:
            with _task_lock:
                initial = json.dumps(list(_tasks))
            yield f"event: init\ndata: {initial}\n\n"
        except GeneratorExit:
            pass

        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in _sse_listeners:
                _sse_listeners.remove(q)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---- Main ----

def main():
    port = 58080
    print(f"🌸 JM 漫画下载器 v2 启动中...")
    print(f"📂 Pictures 目录: {PROJECT_ROOT / 'Pictures'}")
    print(f"📂 PDFs 目录: {PROJECT_ROOT / 'PDFs'}")
    print(f"🌐 浏览器即将打开: http://127.0.0.1:{port}")
    print(f"💡 按 Ctrl+C 停止服务器")
    print()

    (PROJECT_ROOT / "Pictures").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "PDFs").mkdir(parents=True, exist_ok=True)

    def open_browser():
        webbrowser.open(f"http://127.0.0.1:{port}")

    threading.Timer(1.0, open_browser).start()

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
