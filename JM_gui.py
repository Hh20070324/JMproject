import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
os.chdir(str(PROJECT_ROOT))


# -------------------- stdout 重定向到 GUI --------------------
class GuiRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, text):
        self.widget.after(0, self._write, text)

    def _write(self, text):
        self.widget.insert(tk.END, text)
        self.widget.see(tk.END)

    def flush(self):
        pass


# -------------------- 下载线程 --------------------
def download_thread(album_id, btn, root):
    try:
        import jmcomic
        from jpg2pdf import album_to_pdf

        option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
        jmcomic.download_album(album_id, option)

        # 下载完成后自动打包 PDF
        print()
        album_to_pdf(str(PROJECT_ROOT / album_id), str(PROJECT_ROOT))

        print()
        print("=" * 40)
        print("全部完成！PDF 已生成喵~")
        print("=" * 40)
    except Exception as e:
        print()
        print(f"出错: {e}")
    finally:
        btn.after(0, lambda: btn.config(state=tk.NORMAL, text="开始下载"))


# -------------------- 启动下载 --------------------
def start_download(entry, btn, root):
    album_id = entry.get().strip()
    if not album_id:
        return
    btn.config(state=tk.DISABLED, text="下载中...")
    t = threading.Thread(target=download_thread, args=(album_id, btn, root), daemon=True)
    t.start()


# -------------------- 主窗口 --------------------
def main():
    root = tk.Tk()
    root.title("JM 漫画下载器")
    root.geometry("700x500")
    root.resizable(True, True)

    # 顶部框架
    top = ttk.Frame(root, padding=10)
    top.pack(fill=tk.X)

    ttk.Label(top, text="禁漫车号:").pack(side=tk.LEFT)

    entry = ttk.Entry(top, width=20, font=("微软雅黑", 12))
    entry.pack(side=tk.LEFT, padx=8)
    entry.insert(0, "1236513")
    entry.bind("<Return>", lambda e: start_download(entry, btn, root))
    entry.focus()

    btn = ttk.Button(top, text="开始下载", command=lambda: start_download(entry, btn, root))
    btn.pack(side=tk.LEFT, padx=8)

    # 日志输出区域
    log = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Consolas", 10))
    log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    # 重定向 stdout
    redirector = GuiRedirector(log)
    sys.stdout = redirector
    sys.stderr = redirector

    root.mainloop()


if __name__ == "__main__":
    main()
