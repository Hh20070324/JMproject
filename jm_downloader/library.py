import os
import shutil
import threading
import uuid
from pathlib import Path

from .models import LibraryItem
from .pdf import album_to_pdf, find_album_images, is_linked_directory, natural_key
from .settings import AppPaths, DEFAULT_PATHS


class LibraryError(Exception):
    pass


class LibraryNotFound(LibraryError):
    pass


class LibraryService:
    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        self.paths = paths
        self._lock = threading.RLock()
        self.paths.ensure_output_directories()

    def list_items(self) -> list[LibraryItem]:
        with self._lock:
            album_ids = {
                path.name
                for path in self.paths.pictures.iterdir()
                if path.is_dir()
                and not is_linked_directory(path)
                and self._valid_album_id(path.name)
            }
            album_ids.update(
                path.stem
                for path in self.paths.pdfs.glob("*.pdf")
                if path.is_file()
                and not path.is_symlink()
                and self._valid_album_id(path.stem)
            )
            items = []
            for album_id in sorted(album_ids, key=natural_key):
                try:
                    items.append(self.get_item(album_id))
                except LibraryNotFound:
                    continue
            return items

    def get_item(self, album_id: str) -> LibraryItem:
        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_dir(album_id)
            pdf_path = self._pdf_path(album_id)
            images = self._list_images(album_dir)
            try:
                chapter_count = (
                    sum(
                        path.is_dir()
                        and not path.is_symlink()
                        and not is_linked_directory(path)
                        for path in album_dir.iterdir()
                    )
                    if album_dir.is_dir()
                    else 0
                )
                image_size = sum(path.stat().st_size for path in images)
                pdf_exists = pdf_path.is_file() and not pdf_path.is_symlink()
                pdf_size = pdf_path.stat().st_size if pdf_exists else 0
            except OSError as error:
                raise LibraryNotFound("本地漫画文件已发生变化，请刷新后重试") from error

            if not images and not pdf_exists:
                raise LibraryNotFound("未找到该漫画")

            return LibraryItem(
                album_id=album_id,
                chapter_count=chapter_count,
                image_count=len(images),
                image_size=image_size,
                preview_path=images[0] if images else None,
                pdf_path=pdf_path if pdf_exists else None,
                pdf_size=pdf_size,
            )

    def get_preview(self, album_id: str) -> Path:
        with self._lock:
            preview = self.get_item(album_id).preview_path
            if preview is None:
                raise LibraryNotFound("没有可用的预览图")
            return preview

    def get_pdf(self, album_id: str) -> Path:
        with self._lock:
            self._require_album_id(album_id)
            pdf_path = self._pdf_path(album_id)
            if not pdf_path.is_file() or pdf_path.is_symlink():
                raise LibraryNotFound("PDF 不存在")
            return pdf_path

    def rebuild_pdf(self, album_id: str) -> str:
        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_dir(album_id)
            if not self._list_images(album_dir):
                raise LibraryNotFound("没有可用于生成 PDF 的图片")
            try:
                result = album_to_pdf(
                    str(album_dir),
                    str(self.paths.pdfs.resolve()),
                )
            except Exception as error:
                raise LibraryError(f"PDF 生成失败：{error}") from error
            if not result:
                raise LibraryError("PDF 生成失败")
            return result

    def delete_images(self, album_id: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_dir(album_id)
            if not album_dir.is_dir():
                raise LibraryNotFound("图片目录不存在")
            try:
                shutil.rmtree(album_dir)
            except OSError as error:
                raise LibraryError(f"删除图片失败：{error}") from error

    def delete_pdf(self, album_id: str) -> None:
        with self._lock:
            try:
                self.get_pdf(album_id).unlink()
            except OSError as error:
                raise LibraryError(f"删除 PDF 失败：{error}") from error

    def delete_all(self, album_id: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_dir(album_id)
            pdf_path = self._pdf_path(album_id)
            has_images = album_dir.is_dir() and not is_linked_directory(album_dir)
            has_pdf = pdf_path.is_file() and not pdf_path.is_symlink()
            if not has_images and not has_pdf:
                raise LibraryNotFound("未找到该漫画")

            token = uuid.uuid4().hex
            staged = []
            try:
                if has_images:
                    staged_images = album_dir.with_name(
                        f".{album_id}.{token}.delete"
                    )
                    os.replace(album_dir, staged_images)
                    staged.append((staged_images, album_dir, True))
                if has_pdf:
                    staged_pdf = pdf_path.with_name(
                        f".{album_id}.{token}.pdf.delete"
                    )
                    os.replace(pdf_path, staged_pdf)
                    staged.append((staged_pdf, pdf_path, False))
            except OSError as error:
                rollback_errors = []
                for staged_path, original_path, _is_directory in reversed(staged):
                    try:
                        os.replace(staged_path, original_path)
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    details = "; ".join(rollback_errors)
                    raise LibraryError(
                        f"删除漫画失败，且无法完整回滚：{details}"
                    ) from error
                raise LibraryError(f"删除漫画失败：{error}") from error

            cleanup_errors = []
            for staged_path, _original_path, is_directory in staged:
                try:
                    if is_directory:
                        shutil.rmtree(staged_path)
                    else:
                        staged_path.unlink()
                except OSError as error:
                    cleanup_errors.append(str(error))
            if cleanup_errors:
                details = "; ".join(cleanup_errors)
                raise LibraryError(
                    f"漫画已移出本地库，但临时文件清理失败：{details}"
                )

    def open_location(self, album_id: str, kind: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            if kind == "images":
                target = self._album_dir(album_id)
                if not target.is_dir():
                    raise LibraryNotFound("图片目录不存在")
            elif kind == "pdf":
                target = self.get_pdf(album_id)
            else:
                raise LibraryError("不支持的打开类型")

            if not hasattr(os, "startfile"):
                raise LibraryError("当前系统不支持从程序打开文件")
        try:
            os.startfile(target)
        except OSError as error:
            raise LibraryError(f"打开失败：{error}") from error

    def _album_dir(self, album_id: str) -> Path:
        album_dir = self.paths.pictures / album_id
        if is_linked_directory(album_dir):
            raise LibraryNotFound("不支持链接形式的漫画目录")
        resolved = album_dir.resolve()
        if not resolved.is_relative_to(self.paths.pictures.resolve()):
            raise LibraryNotFound("漫画目录不在受管目录中")
        return resolved

    def _pdf_path(self, album_id: str) -> Path:
        pdf_path = self.paths.pdfs / f"{album_id}.pdf"
        if pdf_path.is_symlink():
            raise LibraryNotFound("不支持符号链接形式的 PDF")
        resolved = pdf_path.resolve()
        if not resolved.is_relative_to(self.paths.pdfs.resolve()):
            raise LibraryNotFound("PDF 不在受管目录中")
        return resolved

    def _list_images(self, album_dir: Path) -> list[Path]:
        if not album_dir.is_dir():
            return []
        try:
            return find_album_images(album_dir)
        except OSError as error:
            raise LibraryNotFound("本地漫画文件已发生变化，请刷新后重试") from error

    @staticmethod
    def _valid_album_id(album_id: str) -> bool:
        return album_id.isascii() and album_id.isdigit()

    def _require_album_id(self, album_id: str) -> None:
        if not self._valid_album_id(album_id):
            raise LibraryNotFound("漫画编号无效")
