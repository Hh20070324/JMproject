import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from .pdf import (
    IMAGE_EXTENSIONS,
    PdfPublishAborted,
    PdfSourcePathError,
    is_linked_directory,
    natural_key,
)


def cbz_file_intact(path: str | Path, expected_images: int) -> bool:
    """Offline CBZ structural check.

    The archive must be a readable regular file whose non-directory entry
    count matches ``expected_images`` and whose entry names stay inside
    the archive (no absolute or parent-escaping names).
    """

    candidate = Path(path)
    if type(expected_images) is not int or expected_images < 1:
        raise ValueError("expected_images must be a positive integer")
    try:
        if is_linked_directory(candidate) or not candidate.is_file():
            return False
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None:
                return False
            names = [
                name
                for name in archive.namelist()
                if not name.endswith("/")
            ]
            if len(names) != expected_images:
                return False
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    return False
        return True
    except (OSError, zipfile.BadZipFile):
        return False


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
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for image in images:
                archive.write(image, arcname=image.name)
        if publish_guard is not None and not publish_guard():
            raise PdfPublishAborted()
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
