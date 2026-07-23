import os
import re
import stat
import tempfile
from pathlib import Path

from PIL import Image


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
PART_FILE_MARKER = ".jm-part-"


class PdfPublishAborted(Exception):
    pass


class PdfSourcePathError(OSError):
    """Raised when a chapter PDF source crosses a linked path boundary."""


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


def album_to_pdf(
    album_dir: str,
    output_dir: str | None = None,
    *,
    publish_guard=None,
):
    album_path = Path(album_dir)
    if not album_path.is_dir() or is_linked_directory(album_path):
        print(f"错误: '{album_path}' 不是有效的目录")
        return None

    image_files = find_album_images(album_path)

    return _images_to_pdf(
        image_files,
        album_path.name,
        album_path.parent,
        output_dir,
        publish_guard=publish_guard,
    )


def chapter_to_pdf(
    chapter_dir: str | Path,
    out_path: str | Path,
    publish_guard=None,
):
    """Build one PDF from the direct image children of one chapter directory."""

    chapter_path = Path(chapter_dir)
    if not chapter_path.is_dir():
        raise PdfSourcePathError(f"章节图片目录无效: '{chapter_path}'")
    _reject_linked_components(chapter_path)

    resolved_chapter = chapter_path.resolve(strict=True)
    image_files = []
    for candidate in chapter_path.iterdir():
        if (
            PART_FILE_MARKER in candidate.name
            or candidate.suffix.lower() not in IMAGE_EXTENSIONS
        ):
            continue
        if _is_linked_path(candidate):
            raise PdfSourcePathError(f"章节图片不能是链接: '{candidate}'")
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise PdfSourcePathError(
                f"章节图片无法安全解析: '{candidate}'"
            ) from error
        if not resolved.is_file() or not resolved.is_relative_to(resolved_chapter):
            raise PdfSourcePathError(f"章节图片超出目录边界: '{candidate}'")
        image_files.append(resolved)

    image_files.sort(key=lambda path: natural_key(path.name))
    return _images_to_exact_pdf(
        image_files,
        Path(out_path),
        publish_guard=publish_guard,
    )


def find_album_images(album_path: Path) -> list[Path]:
    if not album_path.is_dir() or is_linked_directory(album_path):
        return []

    resolved_album = album_path.resolve(strict=True)
    image_files = []
    walk_errors = []
    for root, directories, filenames in os.walk(
        album_path,
        followlinks=False,
        onerror=walk_errors.append,
    ):
        root_path = Path(root)
        try:
            resolved_root = root_path.resolve(strict=True)
        except OSError:
            directories[:] = []
            continue
        if not resolved_root.is_relative_to(resolved_album):
            directories[:] = []
            continue

        safe_directories = []
        for name in directories:
            candidate = root_path / name
            if is_linked_directory(candidate):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and resolved.is_relative_to(resolved_album):
                safe_directories.append(name)
        directories[:] = safe_directories

        for filename in filenames:
            candidate = root_path / filename
            if not _is_image(candidate):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_relative_to(resolved_album):
                image_files.append(resolved)

    if walk_errors:
        raise walk_errors[0]
    image_files.sort(
        key=lambda path: tuple(
            natural_key(part) for part in path.relative_to(resolved_album).parts
        )
    )
    return image_files


def is_linked_directory(path: Path) -> bool:
    return _is_linked_path(path)


def _is_linked_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _reject_linked_components(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        if _is_linked_path(current):
            raise PdfSourcePathError(f"章节图片路径不能包含链接: '{current}'")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_image(path: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and PART_FILE_MARKER not in path.name
        and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _images_to_pdf(
    image_files: list[Path],
    pdf_name: str,
    default_output_dir: Path,
    output_dir: str | None,
    *,
    publish_guard=None,
):
    destination = Path(output_dir) if output_dir else default_output_dir
    pdf_path = destination / f"{pdf_name}.pdf"
    return _images_to_exact_pdf(
        image_files,
        pdf_path,
        publish_guard=publish_guard,
    )


def _images_to_exact_pdf(
    image_files: list[Path],
    pdf_path: Path,
    *,
    publish_guard=None,
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

        destination = pdf_path.parent
        destination.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=destination,
            prefix=f".{pdf_path.name}.",
            suffix=".pdf.part",
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            images[0].save(
                temp_path,
                "PDF",
                save_all=True,
                append_images=images[1:],
            )
            if publish_guard is not None and not publish_guard():
                raise PdfPublishAborted("PDF publish was cancelled")
            os.replace(temp_path, pdf_path)
        finally:
            temp_path.unlink(missing_ok=True)
    finally:
        for image in images:
            image.close()

    print(f"PDF 已生成: {pdf_path}")
    return str(pdf_path)
