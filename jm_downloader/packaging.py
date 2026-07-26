import os
from pathlib import Path
import tempfile
import zipfile

from .pdf import (
    IMAGE_EXTENSIONS,
    PdfPublishAborted,
    PdfSourcePathError,
    is_linked_directory,
    natural_key,
)


def chapter_to_cbz(
    chapter_directory: str | Path,
    output_path: str | Path,
    *,
    publish_guard=None,
) -> Path:
    source = Path(chapter_directory)
    target = Path(output_path)
    if (
        not source.is_dir()
        or is_linked_directory(source)
        or target.suffix.lower() != ".cbz"
        or target.is_symlink()
    ):
        raise PdfSourcePathError("CBZ 路径未通过安全检查")

    resolved_source = source.resolve()
    images = []
    for candidate in source.iterdir():
        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or not candidate.resolve().is_relative_to(resolved_source)
        ):
            raise PdfSourcePathError("CBZ 图片路径未通过安全检查")
        images.append(candidate)
    images.sort(key=lambda path: natural_key(path.name))
    if not images:
        raise ValueError("章节目录中没有可打包图片")

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for image in images:
                archive.write(image, arcname=image.name)
        if publish_guard is not None and not publish_guard():
            raise PdfPublishAborted()
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
