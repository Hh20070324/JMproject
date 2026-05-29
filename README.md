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
