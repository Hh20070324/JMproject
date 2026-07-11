# JM 漫画下载器 v2

一个**禁漫天堂漫画下载工具**。输入车号 → 自动下载所有章节图片 → 自动打包成 PDF。

v2 全新升级：**Web 界面 + 批量下载 + 实时进度**。

---

## 第一次使用（只需做一次）

### 第 1 步：一键安装环境

1. 双击 `一键安装.bat`
2. 脚本会检测 Python 3.10–3.14；如果没有兼容版本，会自动运行项目附带的 Python 3.14.5 安装程序并加入 PATH
3. 脚本会在项目中创建独立的 `.venv` 虚拟环境，不会把依赖安装到用户的全局 Python
4. 随后脚本会按照 `requirements.txt` 安装经过验证的固定版本依赖（需要联网，约 1-2 分钟）
5. 脚本完成依赖导入检查后会显示「安装完成」

如果自动安装失败，也可以运行 `setup.bat` 重试并查看具体错误。

### 第 2 步：开启代理（可选但建议）

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
├── app.py                ← 程序入口
├── jm_downloader/        ← API、任务调度、下载及 PDF 模块
├── server.py             ← 旧启动方式兼容入口
├── download_worker.py    ← 旧导入方式兼容模块
├── jpg2pdf.py            ← PDF 命令行兼容入口
├── option.yml            ← 下载配置
├── requirements.txt      ← 固定版本依赖清单
├── static/
│   └── index.html        ← Web 前端界面
├── Pictures/             ← 图片输出（自动创建）
├── PDFs/                 ← PDF 输出（自动创建）
├── python installer/     ← 内置 Python 安装程序
├── .venv/                ← 本机虚拟环境（安装时生成，不参与分发）
├── setup.bat             ← 环境安装实现
├── 一键安装.bat           ← 首次使用时双击
├── 一键下载.bat           ← 启动下载器
└── README.md             ← 本说明文件
```

---

## 常见问题

**Q: 双击 bat 闪退？**
A: 在该文件夹空白处按 `Shift + 右键` → 「在此处打开 PowerShell 窗口」，输入 `.\.venv\Scripts\python.exe app.py` 回车，看报错信息。

**Q: 下载报错 "Restricted Access"？**
A: 你的 IP 被禁漫限制了。开启代理后重试。

**Q: 下载报错类似 ModuleNotFoundError: No module named 'xxxx'？**
A: 项目虚拟环境中的依赖不完整。重新双击 `一键安装.bat` 修复。

**Q: 可以复制到另一台电脑直接使用吗？**
A: 可以复制整个项目，但不要复制 `.venv`。目标电脑应为 64 位 Windows 10/11，并保留 `python installer` 文件夹，然后在目标电脑重新双击 `一键安装.bat`。脚本支持 PATH 中已有的 Python 3.10–3.14；其他版本会改用内置 Python 3.14.5。

**Q: 想改下载速度/图片格式？**
A: 用记事本打开 `option.yml`，修改配置。

---

## 项目信息

- 基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- v2 前端采用日系清新风格设计
- 请勿一次性下载过多本子，爱护禁漫服务器喵~
