from datetime import datetime
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from .models import ChapterManifest, ChapterManifestEntry, LibraryItem
from .pdf import album_to_pdf, find_album_images, is_linked_directory, natural_key
from .settings import AppPaths, DEFAULT_PATHS


class LibraryError(Exception):
    pass


class LibraryNotFound(LibraryError):
    pass


class ChapterManifestError(LibraryError):
    pass


class CorruptChapterManifest(ChapterManifestError):
    pass


class UnsupportedChapterManifestVersion(ChapterManifestError):
    pass


CHAPTER_MANIFEST_FILENAME = ".jm-chapters.json"
CHAPTER_MANIFEST_SCHEMA_VERSION = 1
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ChapterManifestStore:
    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        self.paths = paths
        self.paths.ensure_output_directories()

    def load(self, album_id: str) -> ChapterManifest | None:
        path = self._manifest_path(album_id)
        if path.is_symlink():
            raise ChapterManifestError("不支持链接形式的章节清单")
        if not path.exists():
            return None
        if not path.is_file():
            raise ChapterManifestError("章节清单路径无效")
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ChapterManifestError(f"无法读取章节清单：{error}") from error
        try:
            return self._decode(raw)
        except UnsupportedChapterManifestVersion:
            raise
        except (UnicodeError, json.JSONDecodeError, ChapterManifestError) as error:
            raise CorruptChapterManifest("章节清单内容已损坏") from error

    def merge_and_save(self, incoming: ChapterManifest) -> ChapterManifest:
        self._validate_manifest(incoming)
        path = self._manifest_path(incoming.album_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._require_real_directory(path.parent)

        existing = None
        corrupt = False
        if path.is_symlink():
            raise ChapterManifestError("章节清单路径无效")
        if path.exists():
            if not path.is_file():
                raise ChapterManifestError("章节清单路径无效")
            try:
                raw = path.read_bytes()
            except OSError as error:
                raise ChapterManifestError(
                    f"无法读取章节清单：{error}"
                ) from error
            try:
                existing = self._decode(raw)
            except UnsupportedChapterManifestVersion:
                raise
            except (UnicodeError, json.JSONDecodeError, ChapterManifestError):
                corrupt = True

        merged = self._merge(existing, incoming)
        if corrupt:
            backup = self._next_corrupt_path(path)
            try:
                os.replace(path, backup)
            except OSError as error:
                raise ChapterManifestError(
                    f"章节清单已损坏，且无法保留原文件：{error}"
                ) from error

        self._write_atomic(path, self._encode(merged))
        return merged

    def _manifest_path(self, album_id: str) -> Path:
        album_id = str(album_id)
        if not album_id or not album_id.isascii() or not album_id.isdigit():
            raise ChapterManifestError("章节清单漫画编号无效")
        pictures_root = self.paths.pictures.resolve()
        album_root = self.paths.pictures / album_id
        if is_linked_directory(album_root):
            raise ChapterManifestError("不支持链接形式的漫画目录")
        resolved = album_root.resolve()
        if not resolved.is_relative_to(pictures_root):
            raise ChapterManifestError("章节清单目录不在受管范围内")
        return resolved / CHAPTER_MANIFEST_FILENAME

    @classmethod
    def _merge(
        cls,
        existing: ChapterManifest | None,
        incoming: ChapterManifest,
    ) -> ChapterManifest:
        if existing is None:
            return incoming
        if existing.album_id != incoming.album_id:
            raise ChapterManifestError("章节清单漫画编号不一致")

        by_id = {chapter.photo_id: chapter for chapter in existing.chapters}
        index_owner = {
            chapter.index: chapter.photo_id for chapter in existing.chapters
        }
        for chapter in incoming.chapters:
            previous = by_id.get(chapter.photo_id)
            if previous is not None and (
                previous.index != chapter.index
                or previous.dir_name != chapter.dir_name
            ):
                raise ChapterManifestError("章节身份或目录发生变化")
            owner = index_owner.get(chapter.index)
            if owner is not None and owner != chapter.photo_id:
                raise ChapterManifestError("章节序号发生冲突")
            by_id[chapter.photo_id] = chapter
            index_owner[chapter.index] = chapter.photo_id

        merged = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id=existing.album_id,
            album_title=existing.album_title,
            album_dir_name=existing.album_dir_name,
            chapters=tuple(
                sorted(by_id.values(), key=lambda chapter: chapter.index)
            ),
        )
        cls._validate_manifest(merged)
        return merged

    @classmethod
    def _decode(cls, raw: bytes) -> ChapterManifest:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ChapterManifestError("章节清单根节点必须是对象")
        if set(data) != {
            "version",
            "album_id",
            "album_title",
            "album_dir_name",
            "chapters",
        }:
            raise ChapterManifestError("章节清单字段无效")
        version = data.get("version")
        if type(version) is not int:
            raise ChapterManifestError("章节清单版本无效")
        if version > CHAPTER_MANIFEST_SCHEMA_VERSION:
            raise UnsupportedChapterManifestVersion(
                f"章节清单版本 {version} 高于程序支持的版本"
            )
        if version != CHAPTER_MANIFEST_SCHEMA_VERSION:
            raise ChapterManifestError("不支持的章节清单版本")
        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list):
            raise ChapterManifestError("章节清单条目必须是数组")
        chapters = []
        for value in raw_chapters:
            if not isinstance(value, dict) or set(value) != {
                "photo_id",
                "index",
                "title",
                "dir_name",
                "page_count",
            }:
                raise ChapterManifestError("章节清单条目字段无效")
            chapters.append(
                ChapterManifestEntry(
                    photo_id=value.get("photo_id"),
                    index=value.get("index"),
                    title=value.get("title"),
                    dir_name=value.get("dir_name"),
                    page_count=value.get("page_count"),
                )
            )
        manifest = ChapterManifest(
            version=version,
            album_id=data.get("album_id"),
            album_title=data.get("album_title"),
            album_dir_name=data.get("album_dir_name"),
            chapters=tuple(chapters),
        )
        cls._validate_manifest(manifest)
        return manifest

    @classmethod
    def _encode(cls, manifest: ChapterManifest) -> bytes:
        cls._validate_manifest(manifest)
        data = {
            "version": manifest.version,
            "album_id": manifest.album_id,
            "album_title": manifest.album_title,
            "album_dir_name": manifest.album_dir_name,
            "chapters": [
                {
                    "photo_id": chapter.photo_id,
                    "index": chapter.index,
                    "title": chapter.title,
                    "dir_name": chapter.dir_name,
                    "page_count": chapter.page_count,
                }
                for chapter in manifest.chapters
            ],
        }
        return (
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def _validate_manifest(cls, manifest: ChapterManifest) -> None:
        if not isinstance(manifest, ChapterManifest):
            raise ChapterManifestError("章节清单模型无效")
        if manifest.version != CHAPTER_MANIFEST_SCHEMA_VERSION:
            if (
                type(manifest.version) is int
                and manifest.version > CHAPTER_MANIFEST_SCHEMA_VERSION
            ):
                raise UnsupportedChapterManifestVersion(
                    f"章节清单版本 {manifest.version} 高于程序支持的版本"
                )
            raise ChapterManifestError("章节清单版本无效")
        if (
            not isinstance(manifest.album_id, str)
            or not manifest.album_id
            or not manifest.album_id.isascii()
            or not manifest.album_id.isdigit()
        ):
            raise ChapterManifestError("章节清单漫画编号无效")
        cls._validate_text("漫画标题", manifest.album_title, allow_empty=False)
        cls._validate_component(manifest.album_dir_name)
        if not isinstance(manifest.chapters, tuple):
            raise ChapterManifestError("章节清单条目必须是不可变数组")

        seen_ids = set()
        seen_indexes = set()
        for chapter in manifest.chapters:
            if not isinstance(chapter, ChapterManifestEntry):
                raise ChapterManifestError("章节清单条目模型无效")
            if (
                not isinstance(chapter.photo_id, str)
                or not chapter.photo_id
                or not chapter.photo_id.isascii()
                or not chapter.photo_id.isdigit()
            ):
                raise ChapterManifestError("章节编号无效")
            if chapter.photo_id in seen_ids:
                raise ChapterManifestError("章节编号不能重复")
            seen_ids.add(chapter.photo_id)
            if type(chapter.index) is not int or chapter.index < 1:
                raise ChapterManifestError("章节序号无效")
            if chapter.index in seen_indexes:
                raise ChapterManifestError("章节序号不能重复")
            seen_indexes.add(chapter.index)
            cls._validate_text("章节标题", chapter.title, allow_empty=False)
            if chapter.dir_name not in ("", f"第{chapter.index}章"):
                raise ChapterManifestError("章节目录名无效")
            if type(chapter.page_count) is not int or chapter.page_count < 1:
                raise ChapterManifestError("章节页数无效")

    @staticmethod
    def _validate_text(label: str, value: str, *, allow_empty: bool) -> None:
        if (
            not isinstance(value, str)
            or "\0" in value
            or (not allow_empty and not value.strip())
        ):
            raise ChapterManifestError(f"{label}无效")

    @staticmethod
    def _validate_component(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or value != value.strip(" .")
            or _INVALID_COMPONENT.search(value)
            or Path(value).name != value
            or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise ChapterManifestError("漫画目录名无效")

    @staticmethod
    def _require_real_directory(path: Path) -> None:
        if not path.is_dir() or is_linked_directory(path):
            raise ChapterManifestError("章节清单目录无效")

    @staticmethod
    def _next_corrupt_path(path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = path.with_name(f"{path.name}.corrupt-{stamp}")
        candidate = base
        counter = 1
        while candidate.exists():
            candidate = base.with_name(f"{base.name}-{counter}")
            counter += 1
        return candidate

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temp_path, path)
            except OSError as error:
                raise ChapterManifestError(
                    f"无法写入章节清单：{error}"
                ) from error
        finally:
            temp_path.unlink(missing_ok=True)


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

    def get_pdf_directory(self, album_id: str) -> Path:
        with self._lock:
            self._require_album_id(album_id)
            try:
                manifest = ChapterManifestStore(self.paths).load(album_id)
            except ChapterManifestError as error:
                raise LibraryNotFound("章节清单不可用") from error
            if manifest is None:
                raise LibraryNotFound("章节清单不存在")

            if is_linked_directory(self.paths.pdfs):
                raise LibraryNotFound("不支持链接形式的 PDF 根目录")
            album_root = self.paths.pdfs / album_id
            target = album_root / manifest.album_dir_name
            if (
                is_linked_directory(album_root)
                or is_linked_directory(target)
                or not target.is_dir()
            ):
                raise LibraryNotFound("PDF 储存文件夹不存在")
            resolved = target.resolve()
            try:
                relative = resolved.relative_to(self.paths.pdfs.resolve())
            except ValueError as error:
                raise LibraryNotFound("PDF 目录不在受管范围内") from error
            if (
                len(relative.parts) != 2
                or relative.parts[0] != album_id
                or relative.parts[1] != manifest.album_dir_name
            ):
                raise LibraryNotFound("PDF 目录结构无效")
            try:
                has_pdf = any(
                    path.suffix.lower() == ".pdf"
                    and path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(resolved)
                    for path in target.iterdir()
                )
            except OSError as error:
                raise LibraryNotFound("PDF 储存文件夹无法读取") from error
            if not has_pdf:
                raise LibraryNotFound("PDF 不存在")
            return resolved

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
                try:
                    target = self.get_pdf_directory(album_id)
                except LibraryNotFound:
                    # Phase 3 removes this compatibility fallback when the
                    # local-library scanner fully switches to nested PDFs.
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
