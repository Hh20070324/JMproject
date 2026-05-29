import os
import sys
from pathlib import Path

import jmcomic
from jpg2pdf import album_to_pdf

PROJECT_ROOT = Path(__file__).parent.resolve()
os.chdir(str(PROJECT_ROOT))

# 车号：命令行传入优先，否则用默认值
if len(sys.argv) >= 2:
    ALBUM_ID = sys.argv[1]
else:
    ALBUM_ID = "1236513"

print(f"目标车号: {ALBUM_ID}")
print(f"项目目录: {PROJECT_ROOT}")
print()

option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
jmcomic.download_album(ALBUM_ID, option)

# 下载完成后自动打包为 PDF
print()
album_to_pdf(str(PROJECT_ROOT / ALBUM_ID), str(PROJECT_ROOT))
