const state = {
  tasks: [],
  library: [],
  view: "downloads",
  libraryFilter: "all",
  librarySearch: "",
};

const elements = {
  input: document.getElementById("input-album"),
  add: document.getElementById("btn-add"),
  refresh: document.getElementById("btn-refresh"),
  queue: document.getElementById("queue"),
  library: document.getElementById("library"),
  summary: document.getElementById("summary"),
  connection: document.getElementById("connection"),
  connectionText: document.getElementById("connection-text"),
  search: document.getElementById("library-search"),
  toastRegion: document.getElementById("toast-region"),
};

function escapeHtml(value)
{
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setConnection(status, label)
{
  elements.connection.dataset.state = status;
  elements.connectionText.textContent = label;
}

function showToast(message, isError = false)
{
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " error" : ""}`;
  toast.textContent = message;
  elements.toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), 3200);
}

async function request(url, options = {})
{
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok)
  {
    throw new Error(data.error || "操作失败");
  }
  return data;
}

const eventSource = new EventSource("/api/events");

eventSource.addEventListener("open", () => setConnection("online", "实时更新正常"));
eventSource.addEventListener("init", (event) => {
  state.tasks = JSON.parse(event.data);
  renderTasks();
});
eventSource.addEventListener("message", (event) => handleTaskEvent(JSON.parse(event.data)));
eventSource.onerror = () => setConnection("offline", "实时更新重连中");

function handleTaskEvent(event)
{
  const task = state.tasks.find((item) => item.id === event.id);
  const patches = {
    info: { title: event.title, cover: event.cover, status: "downloading" },
    progress: { progress: event.percent, chapter: event.chapter, status: "downloading" },
    preview: { preview: event.preview },
    completed: { status: "completed", progress: 100, pdf: event.pdf },
    failed: { status: "failed", error: event.error },
    retry: { status: "pending", error: null, progress: 0 },
  };

  if (event.type === "added" && !task)
  {
    state.tasks.push({
      id: event.id,
      album_id: event.album_id,
      title: null,
      cover: null,
      preview: null,
      status: "pending",
      progress: 0,
      chapter: "",
      error: null,
      pdf: null,
    });
  }
  else if (event.type === "removed")
  {
    state.tasks = state.tasks.filter((item) => item.id !== event.id);
  }
  else if (task && patches[event.type])
  {
    Object.assign(task, patches[event.type]);
  }

  if (event.type === "completed")
  {
    showToast(`#${task?.album_id || ""} 下载完成`);
    loadLibrary();
  }
  if (event.type === "failed") showToast(event.error || "下载失败", true);
  renderTasks();
}

async function addAlbum()
{
  const albumId = elements.input.value.trim();
  if (!albumId) return;
  if (!/^\d+$/.test(albumId))
  {
    showToast("车号只能包含数字", true);
    elements.input.focus();
    return;
  }

  elements.add.disabled = true;
  try
  {
    await request("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album_id: albumId }),
    });
    elements.input.value = "";
  }
  catch (error)
  {
    showToast(error.message, true);
  }
  finally
  {
    elements.add.disabled = false;
    elements.input.focus();
  }
}

async function taskAction(id, action)
{
  try
  {
    await request(`/api/${action}/${id}`, { method: action === "remove" ? "DELETE" : "POST" });
  }
  catch (error)
  {
    showToast(error.message, true);
  }
}

function statusLabel(status)
{
  return {
    pending: "等待中",
    fetching: "读取信息",
    downloading: "下载中",
    completed: "已完成",
    failed: "失败",
  }[status] || status;
}

function renderTasks()
{
  document.getElementById("task-count").textContent = state.tasks.length;
  const active = state.tasks.filter((task) => ["fetching", "downloading"].includes(task.status)).length;
  const pending = state.tasks.filter((task) => task.status === "pending").length;
  const completed = state.tasks.filter((task) => task.status === "completed").length;
  const failed = state.tasks.filter((task) => task.status === "failed").length;
  document.getElementById("stat-active").textContent = active;
  document.getElementById("stat-pending").textContent = pending;
  document.getElementById("stat-completed").textContent = completed;
  document.getElementById("stat-failed").textContent = failed;
  elements.summary.hidden = state.tasks.length === 0;

  if (!state.tasks.length)
  {
    elements.queue.innerHTML = `
      <div class="empty-state">
        <strong>下载队列是空的</strong>
        <p>在上方输入车号开始下载</p>
      </div>`;
    return;
  }

  elements.queue.innerHTML = state.tasks.map((task) => {
    const image = task.preview || task.cover;
    const detail = task.status === "failed"
      ? escapeHtml(task.error || "未知错误")
      : task.status === "completed"
        ? "图片与 PDF 已保存到本地"
        : escapeHtml(task.chapter || "准备下载");
    return `
      <article class="task-row" data-status="${escapeHtml(task.status)}">
        ${image
          ? `<img class="task-cover" src="${escapeHtml(image)}" alt="" onerror="this.replaceWith(createCoverPlaceholder())">`
          : `<div class="task-cover cover-placeholder">JM</div>`}
        <div class="task-main">
          <div class="task-kicker">#${escapeHtml(task.album_id)}</div>
          <h2 class="task-title">${escapeHtml(task.title || "正在读取漫画信息")}</h2>
          ${task.status === "failed" ? "" : `
            <div class="progress-track" aria-label="下载进度 ${Number(task.progress) || 0}%">
              <div class="progress-fill" style="width:${Math.max(0, Math.min(100, Number(task.progress) || 0))}%"></div>
            </div>`}
          <div class="task-detail">
            <span class="status status-${escapeHtml(task.status)}">${statusLabel(task.status)}</span>
            <span>${detail}</span>
          </div>
        </div>
        <div class="task-actions">
          ${task.status === "failed" ? `<button class="button button-quiet" onclick="taskAction('${task.id}', 'retry')">重试</button>` : ""}
          ${["completed", "failed"].includes(task.status) ? `<button class="button button-quiet button-danger" onclick="taskAction('${task.id}', 'remove')">移除</button>` : ""}
        </div>
      </article>`;
  }).join("");
}

function createCoverPlaceholder()
{
  const placeholder = document.createElement("div");
  placeholder.className = "task-cover cover-placeholder";
  placeholder.textContent = "JM";
  return placeholder;
}

async function loadLibrary()
{
  elements.refresh.disabled = true;
  try
  {
    state.library = await request("/api/library");
    renderLibrary();
  }
  catch (error)
  {
    elements.library.innerHTML = `<div class="empty-state"><strong>漫画库读取失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
  finally
  {
    elements.refresh.disabled = false;
  }
}

async function libraryAction(albumId, action, method = "POST")
{
  if (action.startsWith("delete-") && !await confirmAction("确定删除这些本地文件吗？此操作无法撤销。")) return;
  const endpointAction = action.replace("delete-", "");
  try
  {
    if (endpointAction.startsWith("open/") && window.pywebview?.api)
    {
      const kind = endpointAction.split("/")[1];
      const result = await window.pywebview.api.open_library_item(albumId, kind);
      if (!result.ok) throw new Error(result.error || "打开失败");
      return;
    }
    await request(`/api/library/${albumId}/${endpointAction}`, { method });
    showToast("操作已完成");
    await loadLibrary();
  }
  catch (error)
  {
    showToast(error.message, true);
  }
}

async function confirmAction(message)
{
  if (window.pywebview?.api)
  {
    return window.pywebview.api.confirm("确认操作", message);
  }
  return window.confirm(message);
}

function formatBytes(bytes)
{
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function renderLibrary()
{
  document.getElementById("library-count").textContent = state.library.length;
  const query = state.librarySearch.trim();
  const items = state.library.filter((item) => {
    const matchesSearch = !query || item.album_id.includes(query);
    const matchesFilter = state.libraryFilter === "all"
      || (state.libraryFilter === "images" && item.has_images)
      || (state.libraryFilter === "pdf" && item.has_pdf);
    return matchesSearch && matchesFilter;
  });

  if (!items.length)
  {
    elements.library.innerHTML = `
      <div class="empty-state">
        <strong>${state.library.length ? "没有匹配的漫画" : "漫画库是空的"}</strong>
        <p>${state.library.length ? "调整搜索或筛选条件" : "完成下载后会自动出现在这里"}</p>
      </div>`;
    return;
  }

  elements.library.innerHTML = items.map((item) => `
    <article class="library-card">
      ${item.preview
        ? `<img class="library-cover" src="${escapeHtml(item.preview)}" alt="漫画 #${escapeHtml(item.album_id)} 预览">`
        : `<div class="library-cover cover-placeholder">PDF</div>`}
      <div class="library-info">
        <h2 class="library-id">#${escapeHtml(item.album_id)}</h2>
        <div class="library-meta">
          <div class="library-meta-row"><span>图片</span><span>${item.image_count} 张 · ${formatBytes(item.image_size)}</span></div>
          <div class="library-meta-row"><span>章节</span><span>${item.chapter_count}</span></div>
          <div class="library-meta-row"><span>PDF</span><span>${item.has_pdf ? formatBytes(item.pdf_size) : "未生成"}</span></div>
        </div>
        <div class="library-actions">
          ${item.has_images ? `<button class="button button-quiet" onclick="libraryAction('${item.album_id}', 'open/images')">打开图片</button>` : ""}
          ${item.has_pdf ? `<button class="button button-quiet" onclick="libraryAction('${item.album_id}', 'open/pdf')">查看 PDF</button>` : ""}
          ${item.has_images ? `<button class="button button-quiet" onclick="libraryAction('${item.album_id}', 'pdf')">${item.has_pdf ? "重建 PDF" : "生成 PDF"}</button>` : ""}
          ${item.has_images ? `<button class="button button-quiet button-danger" onclick="libraryAction('${item.album_id}', 'delete-images', 'DELETE')">删图片</button>` : ""}
          ${item.has_pdf ? `<button class="button button-quiet button-danger" onclick="libraryAction('${item.album_id}', 'delete-pdf', 'DELETE')">删 PDF</button>` : ""}
        </div>
      </div>
    </article>`).join("");
}

function selectView(view)
{
  state.view = view;
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === view);
  });
  document.getElementById("view-downloads").hidden = view !== "downloads";
  document.getElementById("view-library").hidden = view !== "library";
  elements.refresh.hidden = view !== "library";
  if (view === "library") loadLibrary();
}

elements.add.addEventListener("click", addAlbum);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter") addAlbum();
});
elements.refresh.addEventListener("click", loadLibrary);
elements.search.addEventListener("input", () => {
  state.librarySearch = elements.search.value;
  renderLibrary();
});
document.querySelectorAll(".view-tab").forEach((tab) => {
  tab.addEventListener("click", () => selectView(tab.dataset.view));
});
document.querySelectorAll(".segment").forEach((segment) => {
  segment.addEventListener("click", () => {
    state.libraryFilter = segment.dataset.filter;
    document.querySelectorAll(".segment").forEach((item) => item.classList.toggle("active", item === segment));
    renderLibrary();
  });
});

renderTasks();
loadLibrary();
