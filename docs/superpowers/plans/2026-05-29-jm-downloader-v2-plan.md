# JM 漫画下载器 v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor JM comic downloader from Tkinter GUI to a Flask + single-page web app with batch download, auto PDF packing, and file output separation.

**Architecture:** Flask serves a single-page HTML frontend; users add album IDs one-by-one; each ID is verified via jmcomic (title + cover fetch) then queued; max 2 concurrent `download_worker` threads pull from queue; SSE pushes real-time progress to browser; each completed download auto-triggers PDF packing via `jpg2pdf.album_to_pdf`.

**Tech Stack:** Python 3.14, Flask, jmcomic, Pillow, HTML/CSS/JS (vanilla, no framework), EventSource (SSE)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `download_worker.py` | Create | Thread worker: jmcomic download + PDF pack + progress callback |
| `server.py` | Create | Flask app: routes, queue manager, SSE broadcaster, auto-launch browser |
| `static/index.html` | Create | Single-page frontend: input, queue, progress bars, stats |
| `Pictures/` | Create | Image output directory (auto-created on first use) |
| `PDFs/` | Create | PDF output directory (auto-created on first use) |
| `setup.bat` | Modify | Add Flask to pip install line |
| `一键下载.bat` | Modify | Replace `python JM_gui.py` with `python server.py` |
| `README.md` | Modify | Update for v2 usage |
| `option.yml` | Keep | No change needed |
| `jpg2pdf.py` | Keep | Reused as-is via import |
| `JM.py` | Keep (deprecated) | Not removed — legacy reference |
| `JM_gui.py` | Keep (deprecated) | Not removed — legacy reference |
| `手动打包.bat` | Keep (deprecated) | Not removed — legacy reference |

---

### Task 1: Create `download_worker.py` — Download engine with progress

**Files:**
- Create: `download_worker.py`

- [ ] **Step 1: Write the worker module**

```python
import os
import sys
import threading
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
os.chdir(str(PROJECT_ROOT))

import jmcomic
from jpg2pdf import album_to_pdf


class DownloadWorker:
    """
    Wraps jmcomic download + PDF packing into a thread with progress callback.

    Callbacks:
        on_progress(album_id, percent, chapter, page_info)
        on_complete(album_id, pdf_path)
        on_error(album_id, error_message)
        on_info(album_id, title, cover_url)
    """

    def __init__(
        self,
        album_id: str,
        on_progress=None,
        on_complete=None,
        on_error=None,
        on_info=None,
    ):
        self.album_id = str(album_id)
        self.on_progress = on_progress or (lambda *a: None)
        self.on_complete = on_complete or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)
        self.on_info = on_info or (lambda *a: None)
        self._stop_flag = threading.Event()
        self._thread = None

        # Output directories
        self.pictures_dir = PROJECT_ROOT / "Pictures"
        self.pdfs_dir = PROJECT_ROOT / "PDFs"
        self.pictures_dir.mkdir(parents=True, exist_ok=True)
        self.pdfs_dir.mkdir(parents=True, exist_ok=True)

    def fetch_info(self):
        """Fetch album title and cover before downloading. Returns (title, cover_url) or (None, None)."""
        try:
            option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
            client = option.build_jm_client()
            album = client.get_album_detail(self.album_id)
            title = album.title if hasattr(album, "title") else album.name
            cover_path = None
            # Try to get cover URL from album detail
            try:
                cover_path = album.cover if hasattr(album, "cover") else None
            except Exception:
                cover_path = None
            return (title, cover_path)
        except Exception as e:
            return (None, None)

    def run(self):
        """Main download flow, designed to run in a thread."""
        try:
            # Step 1: Fetch info
            title, cover = self.fetch_info()
            self.on_info(self.album_id, title, cover)

            if self._stop_flag.is_set():
                return

            # Step 2: Configure option with custom dir for Pictures/
            option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
            # Override base_dir so images go to Pictures/<album_id>/
            base_rule = option.dir_rule
            album_dir = self.pictures_dir / self.album_id
            album_dir.mkdir(parents=True, exist_ok=True)

            # Use jmcomic's dir_rule override
            import jmcomic.api
            option.dir_rule.base_dir = str(self.pictures_dir)

            # Step 3: Download with progress
            downloaded_count = [0]
            total_photo_count = [1]

            def download_callback(photo, downloader):
                downloaded_count[0] += 1
                if total_photo_count[0] == 1:
                    # Estimate total from first download
                    pass
                pct = min(99, int(downloaded_count[0] / max(1, total_photo_count[0]) * 100))
                chapter = photo.from_album if hasattr(photo, "from_album") else ""
                if hasattr(chapter, "title"):
                    chapter = chapter.title
                self.on_progress(
                    self.album_id,
                    pct,
                    str(chapter) if chapter else "",
                    f"{downloaded_count[0]}",
                )

            jmcomic.download_album(
                self.album_id,
                option,
                callback=download_callback,
            )

            if self._stop_flag.is_set():
                return

            # Step 4: Pack PDF
            self.on_progress(self.album_id, 95, "打包 PDF", "")
            pdf_path = album_to_pdf(str(album_dir), str(self.pdfs_dir))
            self.on_complete(self.album_id, pdf_path)

        except Exception as e:
            traceback.print_exc()
            self.on_error(self.album_id, str(e))

    def start(self):
        """Start download in a background thread."""
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        """Request graceful stop."""
        self._stop_flag.set()
```

- [ ] **Step 2: Commit**

```bash
git add download_worker.py
git commit -m "feat: add DownloadWorker engine with jmcomic + auto PDF packing"
```

---

### Task 2: Create `server.py` — Flask backend with SSE

**Files:**
- Create: `server.py`

- [ ] **Step 1: Write the Flask server**

```python
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
# Each item: {"id": uuid, "album_id": str, "title": str|None, "cover": str|None,
#              "status": "pending"|"fetching"|"downloading"|"completed"|"failed",
#              "progress": int, "chapter": str, "error": str|None, "pdf": str|None}
_task_lock = threading.Lock()
_tasks: list[dict] = []
_events: list[dict] = []  # SSE event buffer
_active_workers: dict[str, DownloadWorker] = {}
MAX_CONCURRENT = 2

# ---- SSE Helpers ----

_sse_listeners: list[queue.Queue] = []


def broadcast_event(event: dict):
    """Push event to all SSE listeners."""
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
    """Start next pending task if slots available."""
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
            return  # no pending tasks

    # Start worker
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

    # Check for duplicates
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

    # Schedule immediately
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
    """SSE endpoint for real-time progress."""

    def event_stream():
        q: queue.Queue = queue.Queue(maxsize=200)
        _sse_listeners.append(q)

        # Send existing tasks as initial state
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

    # Ensure directories
    (PROJECT_ROOT / "Pictures").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "PDFs").mkdir(parents=True, exist_ok=True)

    # Open browser after a short delay
    def open_browser():
        webbrowser.open(f"http://127.0.0.1:{port}")

    threading.Timer(1.0, open_browser).start()

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: add Flask server with SSE, queue manager, and API routes"
```

---

### Task 3: Create `static/index.html` — Day-one fresh style frontend

**Files:**
- Create: `static/index.html` (single file, all CSS/JS inline)

- [ ] **Step 1: Write the frontend HTML**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JM 漫画下载器</title>
<style>
/* ---- Reset & Base ---- */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #fef9f4;
  --card-bg: #ffffff;
  --primary: #e8a0b4;
  --primary-dark: #d4859b;
  --accent: #f2c4d0;
  --text: #4a3f47;
  --text-light: #8c7d85;
  --success: #7bc4a0;
  --danger: #e07b7b;
  --border: #f0e0e0;
  --shadow: 0 2px 12px rgba(0,0,0,0.06);
  --radius: 12px;
}

body {
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* ---- Header ---- */
.header {
  background: linear-gradient(135deg, #f8d0dc 0%, #f0b8c8 50%, #e8a0b4 100%);
  padding: 18px 28px;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow: 0 2px 8px rgba(232,160,180,0.3);
}

.header-icon { font-size: 28px; }

.header h1 { font-size: 20px; font-weight: 600; letter-spacing: 0.5px; }

/* ---- Main Container ---- */
.container {
  max-width: 800px;
  width: 100%;
  margin: 0 auto;
  padding: 24px 20px;
  flex: 1;
}

/* ---- Input Section ---- */
.input-section {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  margin-bottom: 20px;
  display: flex;
  gap: 10px;
  align-items: center;
}

.input-section input {
  flex: 1;
  padding: 12px 16px;
  border: 2px solid var(--border);
  border-radius: 8px;
  font-size: 15px;
  outline: none;
  transition: border-color 0.2s;
  font-family: inherit;
}

.input-section input:focus {
  border-color: var(--primary);
}

.btn {
  padding: 12px 20px;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  font-family: inherit;
  white-space: nowrap;
}

.btn-primary {
  background: var(--primary);
  color: #fff;
}

.btn-primary:hover {
  background: var(--primary-dark);
  transform: translateY(-1px);
}

.btn-sm {
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
}

.btn-retry { background: #fff0d0; color: #b8860b; }
.btn-retry:hover { background: #ffe8a0; }
.btn-remove { background: #f8e0e0; color: var(--danger); }
.btn-remove:hover { background: #f5d0d0; }

/* ---- Queue Section ---- */
.queue-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-light);
}

.empty-state .empty-icon { font-size: 48px; margin-bottom: 12px; }

.empty-state p { font-size: 15px; }

/* ---- Task Card ---- */
.task-card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 16px;
  box-shadow: var(--shadow);
  display: flex;
  gap: 14px;
  align-items: center;
  transition: all 0.3s;
  border-left: 4px solid transparent;
  position: relative;
}

.task-card.status-pending { border-left-color: #d0d0d0; }
.task-card.status-fetching { border-left-color: #f0c040; }
.task-card.status-downloading { border-left-color: var(--primary); }
.task-card.status-completed { border-left-color: var(--success); background: #f6fdf8; }
.task-card.status-failed { border-left-color: var(--danger); background: #fdf6f6; }

.task-cover {
  width: 60px;
  height: 60px;
  border-radius: 6px;
  background: var(--border);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  overflow: hidden;
}

.task-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.task-info { flex: 1; min-width: 0; }

.task-album-id {
  font-size: 12px;
  color: var(--text-light);
  margin-bottom: 2px;
  font-weight: 500;
}

.task-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- Progress Bar ---- */
.progress-bar-wrap {
  height: 6px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 4px;
}

.progress-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
  background: var(--primary);
}

.status-completed .progress-bar-fill { background: var(--success); }
.status-failed .progress-bar-fill { background: var(--danger); }

.progress-detail {
  font-size: 11px;
  color: var(--text-light);
}

.task-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* ---- Status Badge ---- */
.status-badge {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 10px;
  font-weight: 600;
}

.badge-pending { background: #eee; color: #999; }
.badge-fetching { background: #fff5d0; color: #b8860b; }
.badge-downloading { background: #fde8ef; color: #c05070; }
.badge-completed { background: #e0f5ea; color: #4a9a6e; }
.badge-failed { background: #fce0e0; color: #c05050; }

/* ---- Stats Bar ---- */
.stats-bar {
  margin-top: 20px;
  padding: 12px 16px;
  background: var(--card-bg);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  display: flex;
  gap: 20px;
  font-size: 13px;
  color: var(--text-light);
  justify-content: center;
  flex-wrap: wrap;
}

.stats-bar span { white-space: nowrap; }

.stats-bar .count { font-weight: 700; color: var(--text); }

/* ---- Footer ---- */
.footer {
  text-align: center;
  padding: 12px;
  font-size: 11px;
  color: var(--text-light);
  opacity: 0.7;
}

/* ---- Pulse animation for downloading ---- */
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.status-downloading .task-cover { animation: pulse 1.5s ease-in-out infinite; }
</style>
</head>
<body>

<div class="header">
  <span class="header-icon">🌸</span>
  <h1>JM 漫画下载器</h1>
</div>

<div class="container">

  <!-- Input -->
  <div class="input-section">
    <input type="text" id="input-album" placeholder="输入禁漫车号，回车添加..." autofocus>
    <button class="btn btn-primary" id="btn-add">添加</button>
  </div>

  <!-- Queue -->
  <div class="queue-section" id="queue">
    <div class="empty-state" id="empty-state">
      <div class="empty-icon">📭</div>
      <p>还没有下载任务，输入车号开始吧~</p>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-bar" id="stats-bar" style="display:none;">
    <span>⏳ 等待: <span class="count" id="stat-pending">0</span></span>
    <span>📥 下载中: <span class="count" id="stat-downloading">0</span></span>
    <span>✅ 完成: <span class="count" id="stat-completed">0</span></span>
    <span>❌ 失败: <span class="count" id="stat-failed">0</span></span>
  </div>

</div>

<div class="footer">JM 漫画下载器 v2 · Powered by jmcomic</div>

<script>
// ---- State ----
let tasks = [];

// ---- DOM ----
const inputEl = document.getElementById("input-album");
const btnAdd = document.getElementById("btn-add");
const queueEl = document.getElementById("queue");
const emptyState = document.getElementById("empty-state");
const statsBar = document.getElementById("stats-bar");

// ---- SSE Connection ----
const evtSource = new EventSource("/api/events");

evtSource.addEventListener("init", (e) => {
  tasks = JSON.parse(e.data);
  render();
});

evtSource.addEventListener("message", (e) => {
  const event = JSON.parse(e.data);
  handleEvent(event);
});

evtSource.onerror = () => {
  console.warn("SSE connection lost, will auto-reconnect...");
};

// ---- Event Handlers ----
function handleEvent(event)
{
  switch (event.type)
  {
    case "added":
      tasks.push({
        id: event.id,
        album_id: event.album_id,
        title: null,
        cover: null,
        status: "pending",
        progress: 0,
        chapter: "",
        error: null,
        pdf: null,
      });
      break;

    case "info":
      updateTask(event.id, {
        title: event.title,
        cover: event.cover,
        status: "downloading",
      });
      break;

    case "progress":
      updateTask(event.id, {
        progress: event.percent,
        chapter: event.chapter,
        status: "downloading",
      });
      break;

    case "completed":
      updateTask(event.id, {
        status: "completed",
        progress: 100,
        pdf: event.pdf,
      });
      break;

    case "failed":
      updateTask(event.id, {
        status: "failed",
        error: event.error,
      });
      break;

    case "removed":
      tasks = tasks.filter((t) => t.id !== event.id);
      break;

    case "retry":
      updateTask(event.id, {
        status: "pending",
        error: null,
        progress: 0,
      });
      break;
  }
  render();
}

function updateTask(id, patch)
{
  const t = tasks.find((t) => t.id === id);
  if (t)
  {
    Object.assign(t, patch);
  }
}

// ---- Actions ----
function addAlbum()
{
  const albumId = inputEl.value.trim();
  if (!albumId)
  {
    return;
  }

  fetch("/api/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ album_id: albumId }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.error)
      {
        alert(data.error);
      }
      else
      {
        inputEl.value = "";
      }
    })
    .catch((err) => {
      alert("添加失败: " + err.message);
    });

  inputEl.value = "";
  inputEl.focus();
}

function removeTask(id)
{
  fetch("/api/remove/" + id, { method: "DELETE" }).catch(console.error);
}

function retryTask(id)
{
  fetch("/api/retry/" + id, { method: "POST" }).catch(console.error);
}

// ---- Render ----
function getStatusClass(status)
{
  const map = {
    pending: "badge-pending",
    fetching: "badge-fetching",
    downloading: "badge-downloading",
    completed: "badge-completed",
    failed: "badge-failed",
  };
  return map[status] || "";
}

function getStatusText(status)
{
  const map = {
    pending: "等待中",
    fetching: "查询中",
    downloading: "下载中",
    completed: "已完成",
    failed: "失败",
  };
  return map[status] || status;
}

function render()
{
  if (tasks.length === 0)
  {
    emptyState.style.display = "";
    statsBar.style.display = "none";
  }
  else
  {
    emptyState.style.display = "none";
    statsBar.style.display = "";
  }

  // Stats
  const pending = tasks.filter((t) => t.status === "pending").length;
  const downloading = tasks.filter((t) =>
    ["downloading", "fetching"].includes(t.status)
  ).length;
  const completed = tasks.filter((t) => t.status === "completed").length;
  const failed = tasks.filter((t) => t.status === "failed").length;
  document.getElementById("stat-pending").textContent = pending + (downloading > 0 ? `+${downloading}` : "");
  document.getElementById("stat-downloading").textContent = downloading;
  document.getElementById("stat-completed").textContent = completed;
  document.getElementById("stat-failed").textContent = failed;

  // Cards
  queueEl.innerHTML = tasks
    .map(
      (t) => `
    <div class="task-card status-${t.status}">
      <div class="task-cover">
        ${t.cover ? `<img src="${t.cover}" alt="" onerror="this.parentElement.textContent='📖'">` : "📖"}
      </div>
      <div class="task-info">
        <div class="task-album-id">#${t.album_id}</div>
        <div class="task-title">${t.title || "查询中..."}</div>
        ${t.status !== "failed" ? `
        <div class="progress-bar-wrap">
          <div class="progress-bar-fill" style="width:${t.progress}%"></div>
        </div>
        ` : ""}
        <div class="progress-detail">
          <span class="status-badge ${getStatusClass(t.status)}">${getStatusText(t.status)}</span>
          ${t.chapter ? ` · ${t.chapter}` : ""}
          ${t.status === "completed" ? " · ✅ PDF 已生成" : ""}
          ${t.status === "failed" ? ` · ${t.error || "未知错误"}` : ""}
        </div>
      </div>
      <div class="task-actions">
        ${t.status === "failed" ? `<button class="btn btn-sm btn-retry" onclick="retryTask('${t.id}')">重试</button>` : ""}
        ${t.status === "completed" || t.status === "failed" ? `<button class="btn btn-sm btn-remove" onclick="removeTask('${t.id}')">移除</button>` : ""}
      </div>
    </div>
  `
    )
    .join("");

  if (tasks.length === 0)
  {
    queueEl.innerHTML = `
      <div class="empty-state" id="empty-state">
        <div class="empty-icon">📭</div>
        <p>还没有下载任务，输入车号开始吧~</p>
      </div>
    `;
  }
}

// ---- Bind Events ----
btnAdd.addEventListener("click", addAlbum);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter")
  {
    addAlbum();
  }
});

// Initial render
render();
</script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat: add Japanese fresh style single-page frontend with SSE"
```

---

### Task 4: Update `setup.bat` — Add Flask dependency

**Files:**
- Modify: `setup.bat`

- [ ] **Step 1: Update the pip install line**

Change line 29 from:

```bat
python -m pip install jmcomic Pillow -U
```

to:

```bat
python -m pip install jmcomic Pillow Flask -U
```

Use Write tool to regenerate the file with GBK encoding as before. Full file:

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   JM 漫画下载器 - 环境安装
echo ========================================
echo.

:: 检测 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python！
    echo.
    echo 请先安装 Python，安装时务必勾选 "Add python.exe to PATH"
    echo 下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo Python 已检测到:
python --version
echo.

:: 安装依赖
echo 正在安装依赖包 (jmcomic + Pillow + Flask) ...
echo.
python -m pip install jmcomic Pillow Flask -U

if errorlevel 1 (
    echo.
    echo [错误] 安装失败，请检查网络连接后重试
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安裝完成！现在可以双击 "一键下载.bat" 开始使用了喵~
echo ========================================
pause
```

Note: Use GBK encoding with CRLF line endings when writing this file.

- [ ] **Step 2: Commit**

```bash
git add setup.bat
git commit -m "chore: add Flask to setup.bat dependencies"
```

---

### Task 5: Update `一键下载.bat` — Launch Flask server

**Files:**
- Modify: `一键下载.bat`

- [ ] **Step 1: Change launch command**

Replace content with:

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
python server.py
pause
```

Note: Use GBK encoding with CRLF line endings.

- [ ] **Step 2: Commit**

```bash
git add 一键下载.bat
git commit -m "chore: update 一键下载.bat to launch Flask server"
```

---

### Task 6: Update `README.md` — v2 documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite README for v2**

Write the following to `README.md`:

```markdown
# JM 漫画下载器 v2

一个**禁漫天堂漫画下载工具**。输入车号 → 自动下载所有章节图片 → 自动打包成 PDF。

v2 全新升级：**Web 界面 + 批量下载 + 实时进度**。

---

## 第一次使用（只需做一次）

### 第 1 步：安装 Python

如果你电脑上已经装过 Python，跳过这一步。

1. 打开 https://www.python.org/downloads/
2. 点击黄色大按钮下载最新版
3. 运行下载的安装包
4. **务必勾选底部的 `Add python.exe to PATH`**
5. 点击 Install Now，等待安装完成

> 验证方法：按 `Win + R`，输入 `cmd` 回车，在黑色窗口里输入 `python --version` 回车。如果显示 `Python 3.x.x` 就说明装好了。

### 第 2 步：安装依赖包

1. 双击 `setup.bat`
2. 等待自动安装完成（需要联网，约 1-2 分钟）
3. 看到「安装完成」提示后按任意键关闭

### 第 3 步：开启代理（可选但建议）

禁漫对部分 IP 地区有限制。开启 Clash Verge Rev 等代理工具后，程序会自动走系统代理。

---

## 每次使用：下载漫画

1. 双击 `一键下载.bat`
2. 等待浏览器自动打开（如未打开，手动访问 http://127.0.0.1:58080）
3. 在输入框中输入禁漫车号（纯数字，例如 `1236513`），按回车添加
4. 可连续添加多个车号，最多同时下载 2 个
5. 等待进度条走完，显示「✅ PDF 已生成」
6. 图片保存在 `Pictures/` 文件夹，PDF 保存在 `PDFs/` 文件夹

---

## 文件结构

```
JM-Download&wrap_program/
├── server.py             ← Flask 服务器
├── download_worker.py    ← 下载引擎
├── jpg2pdf.py            ← PDF 打包引擎
├── option.yml            ← 下载配置
├── static/
│   └── index.html        ← Web 前端界面
├── Pictures/             ← 图片输出（自动创建）
├── PDFs/                 ← PDF 输出（自动创建）
├── setup.bat             ← 首次安装依赖
├── 一键下载.bat           ← 启动下载器
└── README.md             ← 本说明文件
```

---

## 常见问题

**Q: 双击 bat 闪退？**
A: 在该文件夹空白处按 `Shift + 右键` → 「在此处打开 PowerShell 窗口」，输入 `python server.py` 回车，看报错信息。

**Q: 下载报错 "Restricted Access"？**
A: 你的 IP 被禁漫限制了。开启代理后重试。

**Q: 下载报错类似 ModuleNotFoundError: No module named 'xxxx'？**
A: 依赖没装好。重新双击 `setup.bat` 安装。

**Q: 想改下载速度/图片格式？**
A: 用记事本打开 `option.yml`，修改配置。

---

## 项目信息

- 基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- v2 前端采用日系清新风格设计
- 请勿一次性下载过多本子，爱护禁漫服务器喵~
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README for v2 web interface"
```

---

### Task 7: Integration test — End-to-end verification

**Files:**
- Verify: `download_worker.py`, `server.py`, `static/index.html` all exist and import correctly

- [ ] **Step 1: Test Python imports**

Run:
```bash
python -c "from download_worker import DownloadWorker; print('download_worker OK')"
```
Expected: `download_worker OK`

Run:
```bash
python -c "from server import app; print('server OK')"
```
Expected: `server OK`

- [ ] **Step 2: Verify static file exists**

Run:
```bash
python -c "import pathlib; p=pathlib.Path('static/index.html'); print('index.html exists:', p.exists())"
```
Expected: `index.html exists: True`

- [ ] **Step 3: Start server and verify HTTP response**

Start server in background:
```bash
python server.py &
```
Wait 3 seconds, then:
```bash
curl -s http://127.0.0.1:58080/ | head -5 && echo "--OK--" || echo "--FAIL--"
```
Expected: HTML content and `--OK--`

Test API:
```bash
curl -s http://127.0.0.1:58080/api/queue && echo
```
Expected: `[]`

Stop server:
```bash
taskkill /F /IM python.exe 2>/dev/null; true
```

- [ ] **Step 4: Final commit**

```bash
git add -A && git status --short
git commit -m "test: verify v2 integration - imports, static file, HTTP endpoints"
```
```

---

## Plan Summary

| Task | Component | Files | Approx. Time |
|------|-----------|-------|-------------|
| 1 | Download Worker | Create `download_worker.py` | 10 min |
| 2 | Flask Server | Create `server.py` | 15 min |
| 3 | Frontend UI | Create `static/index.html` | 15 min |
| 4 | Setup script | Modify `setup.bat` | 5 min |
| 5 | Launch script | Modify `一键下载.bat` | 3 min |
| 6 | Documentation | Modify `README.md` | 5 min |
| 7 | Integration test | Verify all files work | 5 min |

**Total: ~1 hour**

---

## Self-Review

1. **Spec coverage:** All sections covered — download worker (spec §4, §8), server with SSE (§6, §8), frontend UI (§12), file structure (§5), setup/launch scripts (§11), directories (§9), error handling (§10). ✅
2. **No placeholders:** All code is complete, no TODOs or "implement later" patterns. ✅
3. **Type consistency:** `DownloadWorker` callbacks in Task 1 match the `on_info/on_progress/on_complete/on_error` signatures used in Task 2. API route paths match the SSE event types in Task 3. ✅
