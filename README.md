# JM 漫画下载器 v2.5.0

一个 Windows 原生漫画下载工具。可以按漫画名、作者、标签或精确 JM 号查找漫画，再将结果加入下载队列；程序会下载全部章节图片、显示首张图片预览，并自动生成 PDF。

v2.5.0 增加账号登录与只读收藏同步。用户可以手动同步默认及自建收藏夹、离线浏览上次同步内容，并直接把收藏漫画加入现有下载队列。密码不会写入磁盘；保存的会话和收藏缓存使用当前 Windows 用户的 DPAPI 加密，并且只写在程序目录。发行版已经包含 Python 与运行依赖，解压即可使用。

## 开箱即用版

1. 下载 `JM-Downloader-v2.5.0-Windows-x64.zip` 及同名 `.sha256` 文件。
2. 在 PowerShell 运行 `Get-FileHash .\JM-Downloader-v2.5.0-Windows-x64.zip -Algorithm SHA256`，确认结果与 `.sha256` 文件一致。
3. 解压 ZIP，保持文件夹结构完整，然后双击 `JM-Downloader.exe`。
4. 首次启动会在程序目录创建 `settings.json`、`Pictures/`、`PDFs/` 和 `logs/`。登录和同步后还会按需创建加密的 `account.dat` 与 `favorites.dat`。

发行目录提供两个入口：

- `JM-Downloader.exe`：正式桌面版，不显示终端窗口，运行日志写入 `logs/app.log`。
- `JM-Downloader-Debug.exe`：调试版，显示终端输出；启动或下载异常时使用。

请整体移动或复制解压后的 `JM-Downloader` 文件夹，不要单独移动 EXE，也不要遗漏 `_internal`。默认图片、PDF 和设置会随程序目录一起迁移；设置为外部绝对路径的下载目录需要单独迁移。

## 源码版运行

源码项目要求 64 位 Windows 和 Python 3.10 至 3.14。推荐双击 `start.bat`，它会在首次运行时调用 `scripts/setup.ps1` 创建项目专用 `.venv` 并安装固定版本依赖，随后通过正式入口 `desktop.py` 启动桌面窗口。

也可以在 PowerShell 中手动运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe desktop.py
```

## 下载漫画

1. 打开程序并进入「搜索与下载」。
2. 切换到「下载任务」标签页，在输入框中填写纯数字 JM 号，例如 `1449491`。
3. 按回车或点击「开始下载」。
4. 继续输入其他 JM 号即可加入队列。
5. 等待任务状态变为「已完成」。

任务开始后会显示标题、进度、状态和已下载的首张漫画图片。任务行支持暂停、继续和取消；失败任务可以继续下载。取消时可以只移除任务并保留文件，也可以同时删除该任务已经下载的图片与 PDF。

下载只复用经过验证的完整图片，损坏或缺失图片会重新下载。全部图片完整后才会生成 PDF。任务完成后会保留约 5 秒，期间可以打开图片目录或使用系统默认 PDF 阅读器查看 PDF，随后任务行自动移除，文件继续由「本地漫画库」管理。

未完成任务保存在程序目录的 `tasks.json`。正常关闭、异常退出或网络失败后，任务会在下次启动时以暂停或失败状态出现；程序不会自动继续联网，只有点击继续后才会补齐缺失图片。

默认最多同时下载 2 个任务，每个任务默认并发下载 16 张图片。两项数值都可以在「设置」中调整，保存后重启程序生效。

也可以在「搜索结果」标签页查找漫画：

- 在左侧选择「综合」「作者」或「标签」，输入关键词后按回车或点击搜索按钮。
- 在右侧输入纯数字或带 `JM` 前缀的编号，进行精确查询。
- 搜索只展示结果和封面，不会自动下载。点击结果卡片的「下载整本」后，漫画才会加入现有下载队列。
- 已加入队列的结果会显示「查看任务」，点击后切换到下载任务列表，不会重复创建任务。

搜索结果按页显示，普通搜索最多展示前 1000 条；网络临时失败时可以重试。当前版本不保存搜索历史，也不提供漫画详情页或章节选择；下载动作始终下载整本漫画。

## 账号与我的收藏

1. 进入「我的收藏」，输入账号和密码后登录。密码只用于当次登录请求，不会保存到设置、日志或账号文件。
2. 点击「同步」后，程序会依次读取默认收藏夹、自建收藏夹和全部分页。只有全部读取成功才会替换本地缓存；断网、停止或中途失败会保留上次同步内容。
3. 同步完成后可以按收藏夹切换和翻页。点击卡片的「下载整本」会加入现有下载队列；已有任务会显示「查看任务」。
4. 程序启动时只恢复本地加密缓存，不会自动连接网站。需要更新收藏时请手动点击「同步」。
5. 点击「退出登录」会在确认后删除程序目录中的 `account.dat` 和 `favorites.dat`。

当前版本的收藏功能是只读同步，不支持添加、取消、移动收藏或管理远端文件夹。会话 Cookie 和收藏元数据整体使用 Windows DPAPI CurrentUser 加密；同一台电脑、同一 Windows 用户移动整个程序目录后仍可读取，但复制到其他电脑或其他 Windows 用户后通常无法解密。遇到“本地登录信息无法读取”时，可以清除本地账号数据并重新登录；程序不会自动删除原密文。

## 本地漫画库

「本地漫画库」会扫描当前设置的图片和 PDF 目录，并支持：

- 按 JM 号搜索。
- 筛选全部、有图片或有 PDF 的项目。
- 打开图片目录，或使用系统默认程序查看 PDF。
- 从已有图片生成或重新生成 PDF。
- 分别删除图片、PDF，或删除全部本地文件。

下载或本地文件操作进行中时，相关项目会暂时禁止冲突操作。需要重新扫描磁盘内容时，点击工具栏中的刷新按钮。

## 设置与文件

常用选项都在桌面窗口的「设置」页管理：

- 图片目录和 PDF 目录。
- 同时下载任务数，范围为 1 至 8。
- 单任务图片并发数，范围为 1 至 64。
- 日志级别。
- 启动页面和窗口尺寸；启动页面可选择搜索与下载、本地漫画库、我的收藏或设置。
- 明亮或黑暗主题。

主题和窗口尺寸会立即更新；下载并发、输出目录、日志级别和启动页面等设置在下次启动后完整生效。设置保存在程序目录的 `settings.json`。程序目录内的路径会保存为相对路径，外部目录会保存为绝对路径。已经创建的未完成任务始终使用创建时的图片和 PDF 目录；修改设置不会把同一任务拆分到新旧目录。

`option.yml` 保留 JMComic 下载器的底层选项，例如图片后缀。图片并发请在设置页修改，不要通过 `option.yml` 调整。请求超时和请求级重试由程序显式设置。

默认运行时文件如下：

| 路径 | 内容 |
|------|------|
| `Pictures/` | 漫画章节图片 |
| `PDFs/` | 生成的 PDF |
| `logs/app.log` | 程序运行日志 |
| `settings.json` | 桌面应用设置 |
| `tasks.json` | 未完成和失败任务的恢复记录 |
| `account.dat` | DPAPI 加密的账号会话；登录后按需创建 |
| `favorites.dat` | DPAPI 加密的完整收藏缓存；同步后按需创建 |
| `option.yml` | 下载器底层配置 |

下载中的图片会短暂使用带 `.jm-part-` 标记的临时文件名；只有完整校验通过后才会原子替换为最终图片。开始或继续该任务时会清理陈旧临时文件，这些文件不会进入预览、本地库或 PDF。

## 项目结构

```text
JM-Download&wrap_program/
├── desktop.py               # Qt 正式入口
├── jm_downloader/
│   ├── qt/                  # 原生窗口、页面、控制器和主题资源
│   ├── tasks.py             # 下载队列与并发控制
│   ├── task_store.py         # 未完成任务的原子持久化
│   ├── account.py           # 账号登录与会话生命周期
│   ├── favorites.py         # 收藏同步与缓存
│   ├── protected_store.py   # Windows DPAPI 便携加密存储
│   ├── downloader.py        # JMComic 下载适配
│   ├── library.py           # 本地漫画库
│   ├── pdf.py               # PDF 生成
│   └── settings.py          # 设置模型与便携路径
├── option.yml               # 下载器底层配置
├── requirements.txt         # 源码运行依赖
├── JM-Downloader.spec       # PyInstaller 构建配置
├── LICENSES/                # 随发行包提供的第三方许可证文本
├── QT_SOURCE_AND_RELINKING.md
├── QT_THIRD_PARTY_NOTICES.txt
├── scripts/
│   ├── setup.ps1            # 源码环境配置
│   └── build.ps1            # Windows 发行包构建
├── start.bat                # 源码快捷启动入口
├── Pictures/                # 默认图片输出，运行时创建
├── PDFs/                    # 默认 PDF 输出，运行时创建
├── settings.json            # 应用设置，运行时创建
├── tasks.json               # 任务恢复记录，按需创建
├── account.dat              # 加密账号会话，按需创建
└── favorites.dat            # 加密收藏缓存，按需创建
```

## 常见问题

**正式版双击后没有窗口？**

运行 `JM-Downloader-Debug.exe` 查看终端错误，并检查 `logs/app.log`。程序目录及设置的输出目录必须可写。

**源码版启动失败？**

在项目目录运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`，确认成功后再运行 `.\.venv\Scripts\python.exe desktop.py`。

**下载报错 `Restricted Access`？**

当前网络出口可能受到目标站点限制。确认网络和代理设置后重试。

**设置文件损坏或路径不可用？**

先退出程序，检查 `settings.json` 中的下载目录。无法修复时可以删除 `settings.json`，程序会在下次启动时恢复默认设置；损坏的配置通常会自动备份为 `settings.json.corrupt-*`。

**关闭窗口后下载是否继续？**

不会在后台继续。确认关闭后，程序会停止调度新图片并等待当前网络请求返回；已经完整取得的在途图片会安全保存。未完成任务写入 `tasks.json`，下次启动时以暂停状态恢复，只有手动点击继续才会联网。

**暂停和取消有什么区别？**

暂停会保留任务和所有已下载文件，稍后点击继续即可补齐。取消会从任务列表移除记录，并让你选择保留文件或同时删除图片与 PDF。删除操作会等待下载线程停止后再执行。

**任务恢复后为什么仍使用旧目录？**

每个任务会记住创建时的图片和 PDF 目录，以免修改设置后把一本漫画拆到两个位置。旧目录任务完成后，可以从任务完成行打开原目录；当前本地库仍只扫描设置中的当前目录。

**复制到另一台电脑后为什么需要重新登录？**

`account.dat` 和 `favorites.dat` 由 Windows DPAPI 绑定到创建它们的 Windows 用户。整体复制程序不会暴露密码，但另一台电脑通常无法解密这两个文件。清除本地账号数据并重新登录即可；图片、PDF、普通设置和任务记录不受此限制。

**登录会影响普通搜索和下载吗？**

不会。v2.5.0 的登录会话只用于账号验证和读取收藏，普通搜索及整本下载仍使用原有客户端链路。

## 构建发行包

准备好项目虚拟环境后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

脚本会安装构建依赖、运行完整测试、构建 PyInstaller `onedir` 目录，验证正式版和调试版，并生成：

- `release/JM-Downloader-v2.5.0-Windows-x64.zip`
- `release/JM-Downloader-v2.5.0-Windows-x64.zip.sha256`

构建不会覆盖保留的 v2.1.0、v2.2.0、v2.3.0 和 v2.4.0 历史发行包，也会拒绝把设置、任务恢复记录、账号会话、收藏缓存、日志、下载内容或临时文件打入 ZIP。

## 合规与免责声明

本项目是独立的第三方工具，与 JMComic 或软件访问的网站运营方不存在隶属、认可、赞助或官方关联。用户有责任确保使用方式符合适用法律、版权要求、网站服务条款和当地规定。本软件不授予用户对第三方内容的任何权利。

Windows 发行版通过 LGPLv3 选项动态加载 Qt 6.11.1、PySide6 Essentials 6.11.1 和 Shiboken6 6.11.1。许可证全文、第三方归属、对应源码及替换库说明随包提供，详见 `LICENSES/`、[QT_SOURCE_AND_RELINKING.md](./QT_SOURCE_AND_RELINKING.md) 和 [QT_THIRD_PARTY_NOTICES.txt](./QT_THIRD_PARTY_NOTICES.txt)。

## License

This project is licensed under the MIT License.

This project contains code derived from
[JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python),
originally created by hect0x7 and licensed under the MIT License.

See [LICENSE](./LICENSE) and
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) for details.
