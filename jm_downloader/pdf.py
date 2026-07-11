import re
from pathlib import Path

from PIL import Image


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def natural_key(name: str):
    parts = re.split(r"(\d+)", name)
    return [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in parts]


def jpg_to_pdf(folder: str, output_dir: str | None = None):
    folder_path = Path(folder)
    if not folder_path.is_dir():
        print(f"错误: '{folder_path}' 不是有效的目录")
        return None

    image_files = sorted(
        (path for path in folder_path.iterdir() if _is_image(path)),
        key=lambda path: natural_key(path.name),
    )
    return _images_to_pdf(image_files, folder_path.name, folder_path.parent, output_dir)


def album_to_pdf(album_dir: str, output_dir: str | None = None):
    album_path = Path(album_dir)
    if not album_path.is_dir():
        print(f"错误: '{album_path}' 不是有效的目录")
        return None

    chapter_dirs = sorted(
        (path for path in album_path.iterdir() if path.is_dir()),
        key=lambda path: natural_key(path.name),
    )
    image_files = []
    for chapter_dir in chapter_dirs:
        image_files.extend(
            sorted(
                (path for path in chapter_dir.iterdir() if _is_image(path)),
                key=lambda path: natural_key(path.name),
            )
        )

    return _images_to_pdf(image_files, album_path.name, album_path.parent, output_dir)


def _is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def _images_to_pdf(
    image_files: list[Path],
    pdf_name: str,
    default_output_dir: Path,
    output_dir: str | None,
):
    if not image_files:
        print("没有找到可用于生成 PDF 的图片文件")
        return None

    print(f"找到 {len(image_files)} 张图片:")
    for image_file in image_files:
        print(f"  {image_file}")

    images = []
    try:
        for image_file in image_files:
            image = Image.open(image_file)
            if image.mode != "RGB":
                image = image.convert("RGB")
            images.append(image)

        destination = Path(output_dir) if output_dir else default_output_dir
        destination.mkdir(parents=True, exist_ok=True)
        pdf_path = destination / f"{pdf_name}.pdf"
        images[0].save(pdf_path, "PDF", save_all=True, append_images=images[1:])
    finally:
        for image in images:
            image.close()

    print(f"PDF 已生成: {pdf_path}")
    return str(pdf_path)
