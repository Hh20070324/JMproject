import os
import shutil
from pathlib import Path

from .pdf import IMAGE_EXTENSIONS, album_to_pdf, natural_key
from .settings import AppPaths, DEFAULT_PATHS


class LibraryError(Exception):
    pass


class LibraryNotFound(LibraryError):
    pass


class LibraryService:
    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        self.paths = paths
        self.paths.ensure_output_directories()

    def list_items(self) -> list[dict]:
        album_ids = {
            path.name
            for path in self.paths.pictures.iterdir()
            if path.is_dir() and self._valid_album_id(path.name)
        }
        album_ids.update(
            path.stem
            for path in self.paths.pdfs.glob("*.pdf")
            if self._valid_album_id(path.stem)
        )
        items = []
        for album_id in sorted(album_ids, key=natural_key):
            try:
                items.append(self.get_item(album_id))
            except LibraryNotFound:
                continue
        return items

    def get_item(self, album_id: str) -> dict:
        self._require_album_id(album_id)
        album_dir = self.paths.pictures / album_id
        pdf_path = self.paths.pdfs / f"{album_id}.pdf"
        images = self._list_images(album_dir)
        chapter_count = (
            sum(path.is_dir() for path in album_dir.iterdir())
            if album_dir.is_dir()
            else 0
        )
        if not images and not pdf_path.is_file():
            raise LibraryNotFound("未找到该漫画")

        image_size = sum(path.stat().st_size for path in images)
        pdf_size = pdf_path.stat().st_size if pdf_path.is_file() else 0
        return {
            "album_id": album_id,
            "chapter_count": chapter_count,
            "image_count": len(images),
            "image_size": image_size,
            "has_images": bool(images),
            "has_pdf": pdf_path.is_file(),
            "pdf_size": pdf_size,
            "preview": (
                f"/api/library/{album_id}/preview" if images else None
            ),
            "pdf": f"/api/library/{album_id}/pdf" if pdf_path.is_file() else None,
        }

    def get_preview(self, album_id: str) -> Path:
        self._require_album_id(album_id)
        images = self._list_images(self.paths.pictures / album_id)
        if not images:
            raise LibraryNotFound("没有可用的预览图")
        return images[0]

    def get_pdf(self, album_id: str) -> Path:
        self._require_album_id(album_id)
        pdf_path = self.paths.pdfs / f"{album_id}.pdf"
        if not pdf_path.is_file():
            raise LibraryNotFound("PDF 不存在")
        return pdf_path

    def rebuild_pdf(self, album_id: str) -> str:
        self._require_album_id(album_id)
        album_dir = self.paths.pictures / album_id
        if not self._list_images(album_dir):
            raise LibraryNotFound("没有可用于生成 PDF 的图片")
        result = album_to_pdf(str(album_dir), str(self.paths.pdfs))
        if not result:
            raise LibraryError("PDF 生成失败")
        return result

    def delete_images(self, album_id: str) -> None:
        self._require_album_id(album_id)
        album_dir = self.paths.pictures / album_id
        if not album_dir.is_dir():
            raise LibraryNotFound("图片目录不存在")
        shutil.rmtree(album_dir)

    def delete_pdf(self, album_id: str) -> None:
        pdf_path = self.get_pdf(album_id)
        pdf_path.unlink()

    def open_location(self, album_id: str, kind: str) -> None:
        self._require_album_id(album_id)
        if kind == "images":
            target = self.paths.pictures / album_id
            if not target.is_dir():
                raise LibraryNotFound("图片目录不存在")
        elif kind == "pdf":
            target = self.get_pdf(album_id)
        else:
            raise LibraryError("不支持的打开类型")

        if not hasattr(os, "startfile"):
            raise LibraryError("当前系统不支持从程序打开文件")
        os.startfile(target)

    @staticmethod
    def _list_images(album_dir: Path) -> list[Path]:
        if not album_dir.is_dir():
            return []
        images = [
            path
            for path in album_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        return sorted(
            images,
            key=lambda path: tuple(
                natural_key(part) for part in path.relative_to(album_dir).parts
            ),
        )

    @staticmethod
    def _valid_album_id(album_id: str) -> bool:
        return album_id.isascii() and album_id.isdigit()

    def _require_album_id(self, album_id: str) -> None:
        if not self._valid_album_id(album_id):
            raise LibraryNotFound("漫画编号无效")
