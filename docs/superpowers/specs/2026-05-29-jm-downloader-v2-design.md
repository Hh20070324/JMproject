# JM 漫画下载器 v2 — 设计文档

> 日期: 2026-05-29
> 版本: 2.0
> 状态: 已确认

## 1. 概述

将现有的 JM 漫画下载器（Python + Tkinter GUI）重构为 Web 前端 + Python 后端架构，支持批量下载、自动打包、输出分流，保持跨电脑可移植性。

## 2. 用户需求

1. GUI 用 Web 前端，日系清新风格，美观
2. 批量下载多个车号 + 自动打包 PDF
3. 图片 → `Pictures/`，PDF → `PDFs/`
4. 可移植性（文件夹复制到其他电脑，setup.bat 安装依赖即可用）

## 3. 设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 架构 | Flask 后端 + 单页 HTML 前端 | 简单够用，不引入额外依赖，Python 全栈 |
| 打包 | PyInstaller `.exe`（可选） | 可移植性额外保障 |
| 视觉风格 | 日系清新风（浅色、樱花粉、圆角卡片） | 用户选择 |
| 页面结构 | 单页队列式 | 用户选择：包含输入区 + 下载队列列表 |
| 车号输入 | 逐个添加（输入框 + 添加按钮） | 用户选择：引导式逐个添加 |
| 信息预查 | 添加后先查禁漫标题/封面 | 用户要求：防止上错车 |
| PDF 生成 | 每本下完自动打包 | 用户选择：即下即打包 |

## 4. 架构

```
浏览器 (前端)           Python 后端 (Flask)         文件系统
─────────────          ──────────────────         ─────────
│ index.html │──HTTP──▶│ server.py           │     │ Pictures/ │
│ CSS+JS     │◀─SSE────│ ├─ /api/add         │     │ PDFs/     │
│ (单页应用)  │         │ ├─ /api/queue       │     └───────────
└────────────┘         │ ├─ /api/events (SSE) │
                        │ ├─ /api/remove/<id> │
                        │ ├─ /api/retry/<id>  │
                        │ ├─ download_worker  │
                        │ └─ jpg2pdf          │
                        └─────────────────────┘
```

## 5. 项目文件结构

```
JM-Download&wrap_program/
├── server.py              # Flask 后端主程序
├── download_worker.py     # 下载工作线程（jmcomic + 进度回调）
├── jpg2pdf.py             # 图片→PDF 引擎（保留，复用）
├── option.yml             # 下载配置（保留）
├── static/
│   └── index.html         # 前端单页（HTML+CSS+JS 全部内联）
├── Pictures/              # 图片输出（自动创建）
├── PDFs/                  # PDF 输出（自动创建）
├── setup.bat              # 一键安装依赖
├── 一键下载.bat            # 启动服务器
└── README.md              # 使用说明（更新）
```

## 6. API 设计

| 路由 | 方法 | 请求体 | 返回 | 说明 |
|------|------|--------|------|------|
| `/` | GET | — | HTML | 前端页面 |
| `/api/add` | POST | `{"album_id": "123456"}` | `{"id": "...", "title": "...", "cover": "..."}` | 添加车号（先查禁漫获取标题/封面） |
| `/api/queue` | GET | — | `[{"id":"...","album_id":"...","title":"...","status":"...","progress":0,"chapter":"..."}, ...]` | 获取队列状态 |
| `/api/remove/<id>` | DELETE | — | `{"ok": true}` | 移除队列项 |
| `/api/retry/<id>` | POST | — | `{"ok": true}` | 重试失败项 |
| `/api/events` | GET | — | SSE 流 | 实时进度推送 |

### SSE 事件类型

```json
{"type": "added", "id": "uuid", "album_id": "123456", "title": "...", "cover": "..."}
{"type": "progress", "id": "uuid", "album_id": "123456", "percent": 45, "chapter": "第3话", "page": "12/30"}
{"type": "completed", "id": "uuid", "album_id": "123456", "pdf": "123456.pdf"}
{"type": "failed", "id": "uuid", "album_id": "123456", "error": "..."}
{"type": "removed", "id": "uuid"}
```

## 7. 任务状态机

```
add ──▶ pending ──▶ fetching_info ──▶ downloading ──▶ completed
                  │                  │               │
                  └──▶ failed        └──▶ failed     └──▶ (自动打包 PDF)
                                      │
                                      └──▶ retry ──▶ downloading
```

## 8. 线程模型

- Flask 主线程：处理 HTTP 请求
- 每个下载任务一个 `threading.Thread`
- 同一时间最多并行下载 2 个（避免封 IP）
- 队列自动调度：一个完成 → 下一个自动开始
- jmcomic 内部多线程下载图片由 `option.yml` 的 `batch_count` 控制

## 9. 目录约定

- `Pictures/<album_id>/<chapter>/00001.jpg` — 图片保持 jmcomic 原有结构
- `PDFs/<album_id>.pdf` — 打包输出
- 所有路径相对项目根目录，无硬编码绝对路径

## 10. 错误处理

- 网络错误：自动重试 3 次（jmcomic 内置）
- 禁漫限制区域：提醒开启代理
- 下载中断：支持单个重试
- PDF 打包失败：记录错误，不阻塞队列

## 11. 可移植性

- 所有路径 `Path(__file__).parent` 相对
- `setup.bat` 一行 `pip install jmcomic Pillow Flask`
- `一键下载.bat` → `python server.py` → 自动打开浏览器
- 整个文件夹 ZIP 复制 → `setup.bat` → 可用
- 可选：`pyinstaller server.py --onefile` 打包成单个 .exe

## 12. 前端 UI 规格

- 日系清新风：浅粉/米白背景，圆角卡片，阴影
- 标题栏：渐变樱花粉色，小图标
- 输入区：输入框 + 「添加」按钮，回车即添加
- 队列卡片：封面缩略图（左）+ 标题 + 进度条 + 状态标记
- 状态色彩：等待（灰）、下载中（粉红脉冲动画）、完成（绿）、失败（红）
- 统计栏：底部固定，显示各状态计数
- 响应式：桌面优先，窗口最小宽度 600px
- 字体：系统默认 + 中文优先（微软雅黑 / PingFang / Noto Sans SC）

## 13. 弃用的旧文件

以下文件在 v2 中不再需要，保留在仓库中做参考：

- `JM.py` — 被 `download_worker.py` 取代
- `JM_gui.py` — 被 `static/index.html` 取代
- `手动打包.bat` — 功能整合到主界面
- `打包专用/` — 不再需要

## 14. 待实现步骤

1. 创建 `download_worker.py` — 封装 jmcomic 下载 + jpg2pdf 打包 + 进度回调
2. 创建 `server.py` — Flask 后端（API + SSE + 队列管理）
3. 创建 `static/index.html` — 日系清新风前端单页
4. 更新 `setup.bat` — 加入 Flask 依赖
5. 更新 `一键下载.bat` — 改为启动 Flask 服务器
6. 更新 `README.md`
7. 测试：添加车号 → 下载 → PDF 生成 → 验证目录结构
