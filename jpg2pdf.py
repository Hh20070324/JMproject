import sys
import os
import re
from pathlib import Path
from PIL import Image


def natural_key(name: str):
    """
    提取文件名中的数字用于自然排序。
    例如: [00001, 00002, 00010, 00100] 而非 [00001, 00002, 00010, 00100]
    """
    parts = re.split(r'(\d+)', name)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p.lower()))
    return key


def jpg_to_pdf(folder: str, output_dir: str | None = None):
    folder = Path(folder)
    if not folder.is_dir():
        print(f"错误: '{folder}' 不是有效的目录")
        return

    # 收集当前目录下所有图片（不含子目录）
    img_exts = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')
    img_files = []
    seen = set()
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in img_exts:
            if f.name not in seen:
                seen.add(f.name)
                img_files.append(f)

    if not img_files:
        print(f"目录 '{folder}' 中没有找到图片文件")
        return

    # 按数字顺序排序
    img_files.sort(key=lambda f: natural_key(f.name))

    print(f"找到 {len(img_files)} 张图片:")
    for f in img_files:
        print(f"  {f.name}")

    # 打开所有图片并转为 RGB
    images = []
    for f in img_files:
        img = Image.open(f)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        images.append(img)

    # 以第一张图为基准，其余追加
    pdf_name = folder.name + '.pdf'
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        pdf_path = out / pdf_name
    else:
        pdf_path = folder.parent / pdf_name

    if len(images) == 1:
        images[0].save(pdf_path, 'PDF')
    else:
        images[0].save(pdf_path, 'PDF', save_all=True, append_images=images[1:])

    # 关闭所有图片
    for img in images:
        img.close()

    print(f"PDF 已生成: {pdf_path}")
    return str(pdf_path)


def album_to_pdf(album_dir: str, output_dir: str = None):
    """
    遍历 album_dir 下所有章节子目录，
    按章节→页码自然排序，合并所有图片为单个 PDF。
    PDF 命名为 {album_dir.name}.pdf，输出到 output_dir（默认为 album_dir 的父目录）。
    """
    album_dir = Path(album_dir)
    if not album_dir.is_dir():
        print(f"错误: '{album_dir}' 不是有效的目录")
        return

    # 扫描所有章节子目录（自然排序）
    img_exts = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')
    chapter_dirs = sorted(
        [d for d in album_dir.iterdir() if d.is_dir()],
        key=lambda d: natural_key(d.name)
    )

    if not chapter_dirs:
        print(f"目录 '{album_dir}' 中没有找到章节子目录")
        return

    # 收集所有图片：按章节→页码排序
    all_img_files = []
    for ch_dir in chapter_dirs:
        imgs = sorted(
            [f for f in ch_dir.iterdir() if f.is_file() and f.suffix.lower() in img_exts],
            key=lambda f: natural_key(f.name)
        )
        all_img_files.extend(imgs)

    if not all_img_files:
        print(f"所有章节子目录中都没有找到图片文件")
        return

    print(f"找到 {len(chapter_dirs)} 个章节，共 {len(all_img_files)} 张图片:")
    for f in all_img_files:
        print(f"  {f.relative_to(album_dir)}")

    # 打开所有图片并转为 RGB
    images = []
    for f in all_img_files:
        img = Image.open(f)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        images.append(img)

    # 生成 PDF
    pdf_name = album_dir.name + '.pdf'
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        pdf_path = out / pdf_name
    else:
        pdf_path = album_dir.parent / pdf_name

    if len(images) == 1:
        images[0].save(pdf_path, 'PDF')
    else:
        images[0].save(pdf_path, 'PDF', save_all=True, append_images=images[1:])

    # 关闭所有图片
    for img in images:
        img.close()

    print(f"PDF 已生成: {pdf_path}")
    return str(pdf_path)


if __name__ == '__main__':
    if len(sys.argv) >= 2:
        jpg_to_pdf(sys.argv[1])
    else:
        # 默认：打包专用 → 项目目录
        project_root = Path(__file__).parent.resolve()
        jpg_to_pdf(
            str(project_root / "打包专用"),
            str(project_root)
        )
