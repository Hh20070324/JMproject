# JM 漫画下载器 v2.1

一个**禁漫天堂漫画下载工具**。输入车号 → 自动下载所有章节图片 → 自动打包成 PDF。

当前版本提供 **Windows 桌面窗口 + 批量下载 + 实时进度 + 本地漫画库**。

## 开箱即用版

从发行包解压 `JM-Downloader-v2.1.0-Windows-x64.zip`，双击 `JM-Downloader.exe` 即可运行。发行版已经包含 Python 和全部依赖，不需要安装 Python，也不需要联网安装运行环境。

发行目录提供两个入口：

- `JM-Downloader.exe`：正式桌面版，不显示终端窗口，错误写入 `logs/app.log`。
- `JM-Downloader-Debug.exe`：调试版，显示终端日志；遇到启动或下载问题时使用。

`Pictures/`、`PDFs/` 和 `option.yml` 位于程序目录。更新版本时保留这三个位置，即可继续使用原有下载内容和设置。

---

## 源码版运行

以下内容仅适用于源码项目。双击 `start.bat`，程序会自动检测运行环境；首次使用时会调用 `scripts/setup.ps1` 创建 `.venv` 并安装固定版本依赖。如果没有兼容的 Python 3.10–3.14，会使用项目附带的 Python 3.14.5 安装程序。

### 开启代理（可选但建议）

禁漫对部分 IP 地区有限制。开启 Clash Verge Rev 等代理工具后，程序会自动走系统代理。

---

## 下载漫画

1. 发行版双击 `JM-Downloader.exe`；源码版双击 `start.bat`
2. 等待独立桌面窗口打开
3. 在输入框中输入禁漫车号（纯数字，例如 `1236513`），按回车添加
4. 可连续添加多个车号，最多同时下载 2 个
5. 等待任务状态变为「已完成」
6. 图片保存在 `Pictures/` 文件夹，PDF 保存在 `PDFs/` 文件夹

切换到桌面窗口中的「漫画库」，可以管理程序启动前已经存在的下载内容，并分别打开、重新生成或删除图片和 PDF。

桌面版会通过 Windows 原生能力打开图片目录和 PDF，并使用原生确认对话框。启动及异常信息记录在程序目录的 `logs/app.log`。

---

## 文件结构

```
JM-Download&wrap_program/
├── desktop.py            ← 桌面窗口入口
├── app.py                ← 浏览器调试入口
├── jm_downloader/        ← API、任务调度、下载、PDF 及漫画库模块
├── server.py             ← 旧启动方式兼容入口
├── download_worker.py    ← 旧导入方式兼容模块
├── jpg2pdf.py            ← PDF 命令行兼容入口
├── option.yml            ← 下载配置
├── requirements.txt      ← 固定版本依赖清单
├── static/
│   ├── index.html        ← Web 前端结构
│   ├── app.css           ← 界面样式
│   └── app.js            ← 前端交互
├── scripts/
│   └── setup.ps1         ← 环境配置实现
├── Pictures/             ← 图片输出（自动创建）
├── PDFs/                 ← PDF 输出（自动创建）
├── python installer/     ← 内置 Python 安装程序
├── .venv/                ← 本机虚拟环境（安装时生成，不参与分发）
├── start.bat             ← 唯一双击入口
└── README.md             ← 本说明文件
```

---

## 常见问题

**Q: 正式版双击后没有窗口？**
A: 运行 `JM-Downloader-Debug.exe` 查看终端错误，同时检查 `logs/app.log`。

**Q: 源码版 start.bat 闪退？**
A: 在项目目录打开 PowerShell，运行 `.\.venv\Scripts\python.exe desktop.py` 查看错误。浏览器调试模式可以运行 `.\.venv\Scripts\python.exe app.py`。

**Q: 下载报错 "Restricted Access"？**
A: 你的 IP 被禁漫限制了。开启代理后重试。

**Q: 下载报错类似 ModuleNotFoundError: No module named 'xxxx'？**
A: 项目虚拟环境中的依赖不完整。删除 `.venv` 后重新双击 `start.bat` 修复。

**Q: 可以复制到另一台电脑直接使用吗？**
A: 发行版可以，解压 ZIP 后整体复制 `JM-Downloader` 文件夹，不要遗漏 `_internal`。源码版不要复制 `.venv`，应在目标电脑重新运行 `start.bat`。

**Q: 想改下载速度/图片格式？**
A: 用记事本打开 `option.yml`，修改配置。

---

## 项目信息

- 基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- v2.1 使用 pywebview + WebView2 提供独立桌面窗口
- 请勿一次性下载过多本子，爱护禁漫服务器喵~

## 构建发行包

开发环境安装完成后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

脚本会运行测试、构建 PyInstaller `onedir` 目录，并生成 `release/JM-Downloader-v2.1.0-Windows-x64.zip`。

## License

This project is licensed under the MIT License.

This project contains code derived from
[JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python),
originally created by hect0x7 and licensed under the MIT License.

See [LICENSE](./LICENSE) and
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) for details.
