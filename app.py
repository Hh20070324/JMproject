import os
import sys
import threading
import webbrowser

from jm_downloader.application import create_app
from jm_downloader.settings import DEFAULT_PATHS


app = create_app()


def main():
    port = 58080
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("JM 漫画下载器启动中...")
    print(f"图片目录: {DEFAULT_PATHS.pictures}")
    print(f"PDF 目录: {DEFAULT_PATHS.pdfs}")
    print(f"浏览器即将打开: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务器")
    print()

    if os.environ.get("JM_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
