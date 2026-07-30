from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from jmcomic import fix_windir_name
from PIL import Image, UnidentifiedImageError

from .models import (
    ChapterImageStatus,
    ChapterCatalogSnapshot,
    ChapterDeleteResult,
    ChapterManifest,
    ChapterManifestEntry,
    ChapterOperationFailure,
    ChapterPackageStatus,
    ChapterRepairBatch,
    ChapterRepairPlan,
    ChapterRebuildOutcome,
    ChapterRebuildResult,
    ChapterSnapshot,
    LibraryChapterSnapshot,
    LibraryItem,
    LibraryLayout,
    LegacyChapterMapping,
    LegacyMigrationPlan,
    LocalReadProbeSnapshot,
    LocalReadProbeState,
)
from .packaging import cbz_file_intact, chapter_to_cbz
from .pdf import (
    IMAGE_EXTENSIONS,
    PART_FILE_MARKER,
    chapter_to_pdf,
    find_album_images,
    is_linked_directory,
    natural_key,
    pdf_file_readable,
)
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
CHAPTER_MANIFEST_SCHEMA_VERSION = 3
_MANAGED_PATH_LIMIT = 240
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
    def __init__(
        self,
        paths: AppPaths = DEFAULT_PATHS,
        *,
        ensure_directories: bool = True,
    ):
        self.paths = paths
        if ensure_directories:
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
        incoming = self._upgrade_manifest(incoming)
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

    def replace_exact(self, manifest: ChapterManifest) -> ChapterManifest:
        """Atomically publish an exact manifest without merging old chapters.

        This is intentionally narrower than ``merge_and_save`` and is used by
        managed library transactions that must preserve removals or per-chapter
        metadata changes exactly.
        """

        manifest = self._upgrade_manifest(manifest)
        self._validate_manifest(manifest)
        path = self._manifest_path(manifest.album_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._require_real_directory(path.parent)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ChapterManifestError("章节清单路径无效")
        self._write_atomic(path, self._encode(manifest))
        return manifest

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
        if version not in {1, 2, CHAPTER_MANIFEST_SCHEMA_VERSION}:
            raise ChapterManifestError("不支持的章节清单版本")
        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list):
            raise ChapterManifestError("章节清单条目必须是数组")
        chapters = []
        for value in raw_chapters:
            expected_fields = {
                "photo_id",
                "index",
                "title",
                "dir_name",
                "page_count",
            }
            if version >= 2:
                expected_fields.update(
                    {"image_format", "downloaded_at_utc"}
                )
            if version >= 3:
                expected_fields.add("package_format")
            if not isinstance(value, dict) or set(value) != expected_fields:
                raise ChapterManifestError("章节清单条目字段无效")
            chapters.append(
                ChapterManifestEntry(
                    photo_id=value.get("photo_id"),
                    index=value.get("index"),
                    title=value.get("title"),
                    dir_name=value.get("dir_name"),
                    page_count=value.get("page_count"),
                    image_format=value.get("image_format"),
                    downloaded_at_utc=value.get("downloaded_at_utc"),
                    package_format=value.get("package_format"),
                )
            )
        manifest = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id=data.get("album_id"),
            album_title=data.get("album_title"),
            album_dir_name=data.get("album_dir_name"),
            chapters=tuple(chapters),
        )
        cls._validate_manifest(manifest)
        return manifest

    @classmethod
    def _encode(cls, manifest: ChapterManifest) -> bytes:
        manifest = cls._upgrade_manifest(manifest)
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
                    "image_format": chapter.image_format,
                    "downloaded_at_utc": chapter.downloaded_at_utc,
                    "package_format": chapter.package_format,
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
            if chapter.image_format not in {None, "jpg", "png"}:
                raise ChapterManifestError("章节图片格式无效")
            if chapter.package_format not in {None, "pdf", "cbz", "images"}:
                raise ChapterManifestError("章节打包格式无效")
            if chapter.downloaded_at_utc is not None:
                if (
                    not isinstance(chapter.downloaded_at_utc, str)
                    or not chapter.downloaded_at_utc.endswith("Z")
                ):
                    raise ChapterManifestError("章节下载时间无效")
                try:
                    datetime.fromisoformat(
                        chapter.downloaded_at_utc[:-1] + "+00:00"
                    )
                except ValueError as error:
                    raise ChapterManifestError(
                        "章节下载时间无效"
                    ) from error

    @staticmethod
    def _upgrade_manifest(manifest: ChapterManifest) -> ChapterManifest:
        if (
            isinstance(manifest, ChapterManifest)
            and type(manifest.version) is int
            and 1 <= manifest.version < CHAPTER_MANIFEST_SCHEMA_VERSION
        ):
            return ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id=manifest.album_id,
                album_title=manifest.album_title,
                album_dir_name=manifest.album_dir_name,
                chapters=manifest.chapters,
            )
        return manifest

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
            album_ids = self._safe_album_ids(self.paths.pictures)
            album_ids.update(self._safe_album_ids(self.paths.pdfs))
            items = []
            for album_id in sorted(album_ids, key=natural_key):
                try:
                    items.append(self.get_item(album_id))
                except LibraryNotFound:
                    continue
            return items

    def completed_chapter_ids(self, album_id: str) -> frozenset[str]:
        """Return chapters whose manifest and image files are both complete."""

        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_directory(
                self.paths.pictures,
                album_id,
            )
            if not album_dir.is_dir():
                return frozenset()
            try:
                manifest = ChapterManifestStore(self.paths).load(album_id)
            except ChapterManifestError:
                return frozenset()
            if manifest is None:
                return frozenset()
            title_dir = self._safe_child_directory(
                album_dir,
                manifest.album_dir_name,
            )
            if title_dir is None:
                return frozenset()

            completed = set()
            for chapter in manifest.chapters:
                chapter_dir = title_dir
                if chapter.dir_name:
                    chapter_dir = self._safe_child_directory(
                        title_dir,
                        chapter.dir_name,
                    )
                    if chapter_dir is None:
                        continue
                try:
                    images = self._list_direct_images(chapter_dir)
                except LibraryNotFound:
                    continue
                if (
                    chapter.page_count <= 0
                    or len(images) != chapter.page_count
                    or not self._images_match_format(
                        images,
                        chapter.image_format,
                    )
                    or not all(
                        self._is_valid_image_file(path)
                        for path in images
                    )
                ):
                    continue
                completed.add(chapter.photo_id)
            return frozenset(completed)

    def check_chapters(
        self,
        album_id: str,
    ) -> tuple[LibraryChapterSnapshot, ...]:
        """Offline per-chapter status for one managed album.

        Reads only manifest-declared managed paths, never touches the
        network, and never writes the manifest back.  Each chapter is
        checked independently so one failure cannot hide the others.
        """

        with self._lock:
            self._require_album_id(album_id)
            try:
                manifest = ChapterManifestStore(self.paths).load(album_id)
            except ChapterManifestError as error:
                raise LibraryError(
                    "章节清单不可用，无法检查章节"
                ) from error
            if manifest is None:
                raise LibraryNotFound("没有可用的章节清单")
            return self._check_manifest_chapters(album_id, manifest)

    def probe_local_read(self, album_id: str) -> LocalReadProbeSnapshot:
        """Inspect managed local images without writing or using the network."""

        with self._lock:
            self._require_album_id(album_id)
            try:
                manifest = ChapterManifestStore(
                    self.paths,
                    ensure_directories=False,
                ).load(album_id)
            except ChapterManifestError as error:
                raise LibraryError(
                    "章节清单不可用，无法检查本地阅读内容"
                ) from error
            if manifest is None:
                return LocalReadProbeSnapshot(
                    album_id=str(album_id),
                    state=LocalReadProbeState.ABSENT,
                )

            snapshots = self._check_manifest_chapters(album_id, manifest)
            complete = tuple(
                ChapterSnapshot(
                    photo_id=value.photo_id,
                    index=value.index,
                    title=value.title,
                    downloaded=True,
                )
                for value in snapshots
                if value.image_status is ChapterImageStatus.COMPLETE
            )
            if complete:
                return LocalReadProbeSnapshot(
                    album_id=str(album_id),
                    state=LocalReadProbeState.READY,
                    chapters=complete,
                )
            if any(
                "check_error" in value.problem_codes
                for value in snapshots
            ):
                raise LibraryError("本地章节检查失败，请稍后重试")
            return LocalReadProbeSnapshot(
                album_id=str(album_id),
                state=LocalReadProbeState.UNAVAILABLE,
            )

    def _check_manifest_chapters(
        self,
        album_id: str,
        manifest: ChapterManifest,
    ) -> tuple[LibraryChapterSnapshot, ...]:
        album_dir = self._album_directory(self.paths.pictures, album_id)
        pdf_album_dir = self._album_directory(self.paths.pdfs, album_id)
        title_dir = self._safe_child_directory(
            album_dir,
            manifest.album_dir_name,
        )
        package_dir = self._safe_child_directory(
            pdf_album_dir,
            manifest.album_dir_name,
        )
        snapshots = []
        for chapter in sorted(
            manifest.chapters,
            key=lambda value: value.index,
        ):
            try:
                snapshots.append(
                    self._check_chapter(
                        album_id,
                        manifest,
                        chapter,
                        title_dir,
                        package_dir,
                    )
                )
            except (LibraryError, OSError):
                snapshots.append(
                    self._chapter_check_failed(album_id, chapter)
                )
        return tuple(snapshots)

    def _check_chapter(
        self,
        album_id: str,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
        title_dir: Path | None,
        package_dir: Path | None,
    ) -> LibraryChapterSnapshot:
        image_directory = self._chapter_image_directory(title_dir, chapter)
        images: tuple[Path, ...] = ()
        listing_failed = False
        if image_directory is not None:
            try:
                images = self._list_direct_images(image_directory)
            except LibraryNotFound:
                listing_failed = True
        valid_count = sum(
            1 for path in images if self._is_valid_image_file(path)
        )

        problem_codes = []
        if listing_failed:
            image_status = ChapterImageStatus.DAMAGED
            problem_codes.append("check_error")
        elif image_directory is None or len(images) < chapter.page_count:
            image_status = ChapterImageStatus.MISSING
            problem_codes.append("images_missing")
        elif (
            len(images) != chapter.page_count
            or valid_count != len(images)
            or not self._images_match_format(images, chapter.image_format)
        ):
            image_status = ChapterImageStatus.DAMAGED
            problem_codes.append("images_damaged")
        else:
            image_status = ChapterImageStatus.COMPLETE

        pdf_path = self._chapter_package_path(
            package_dir,
            manifest,
            chapter,
            "pdf",
        )
        cbz_path = self._chapter_package_path(
            package_dir,
            manifest,
            chapter,
            "cbz",
        )
        pdf_exists = pdf_path is not None and pdf_path.is_file()
        cbz_exists = cbz_path is not None and cbz_path.is_file()

        package_format = chapter.package_format
        suggested = None
        package_path = None
        if package_format is None:
            package_status = ChapterPackageStatus.UNKNOWN
            if pdf_exists and not cbz_exists:
                suggested = "pdf"
            elif cbz_exists and not pdf_exists:
                suggested = "cbz"
            problem_codes.append("format_unknown")
        elif package_format == "images":
            package_status = ChapterPackageStatus.NOT_APPLICABLE
        else:
            package_path = pdf_path if package_format == "pdf" else cbz_path
            exists = pdf_exists if package_format == "pdf" else cbz_exists
            if not exists:
                package_status = ChapterPackageStatus.MISSING
                problem_codes.append("package_missing")
            else:
                intact = (
                    pdf_file_readable(package_path)
                    if package_format == "pdf"
                    else cbz_file_intact(package_path, chapter.page_count)
                )
                if intact:
                    package_status = ChapterPackageStatus.COMPLETE
                else:
                    package_status = ChapterPackageStatus.DAMAGED
                    problem_codes.append("package_damaged")

        return LibraryChapterSnapshot(
            album_id=album_id,
            photo_id=chapter.photo_id,
            index=chapter.index,
            title=chapter.title,
            image_directory=image_directory,
            package_path=package_path if package_path is not None else None,
            page_count=chapter.page_count,
            valid_image_count=valid_count,
            image_status=image_status,
            package_format=package_format,
            package_status=package_status,
            downloaded_at_utc=chapter.downloaded_at_utc,
            can_rebuild=(
                image_status is ChapterImageStatus.COMPLETE
                and package_format in {"pdf", "cbz"}
                and package_status
                in {
                    ChapterPackageStatus.MISSING,
                    ChapterPackageStatus.DAMAGED,
                }
            ),
            can_redownload=(
                image_status is not ChapterImageStatus.COMPLETE
            ),
            can_delete_images=(
                image_status is not ChapterImageStatus.MISSING
            ),
            can_delete_package=(
                package_format != "images" and (pdf_exists or cbz_exists)
            ),
            can_delete_all=True,
            suggested_package_format=suggested,
            problem_codes=tuple(problem_codes),
        )

    @staticmethod
    def _chapter_check_failed(
        album_id: str,
        chapter: ChapterManifestEntry,
    ) -> LibraryChapterSnapshot:
        return LibraryChapterSnapshot(
            album_id=album_id,
            photo_id=chapter.photo_id,
            index=chapter.index,
            title=chapter.title,
            image_directory=None,
            package_path=None,
            page_count=chapter.page_count,
            valid_image_count=0,
            image_status=ChapterImageStatus.DAMAGED,
            package_format=chapter.package_format,
            package_status=ChapterPackageStatus.UNKNOWN,
            downloaded_at_utc=chapter.downloaded_at_utc,
            can_rebuild=False,
            can_redownload=True,
            can_delete_images=False,
            can_delete_package=False,
            can_delete_all=True,
            suggested_package_format=None,
            problem_codes=("check_error",),
        )

    def _chapter_image_directory(
        self,
        title_dir: Path | None,
        chapter: ChapterManifestEntry,
    ) -> Path | None:
        if title_dir is None:
            return None
        if not chapter.dir_name:
            return title_dir
        return self._safe_child_directory(title_dir, chapter.dir_name)

    @staticmethod
    def _chapter_package_path(
        package_dir: Path | None,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
        suffix: str,
    ) -> Path | None:
        if package_dir is None:
            return None
        name = chapter.dir_name or manifest.album_dir_name
        candidate = package_dir / f"{name}.{suffix}"
        if is_linked_directory(candidate):
            return None
        if not candidate.exists():
            return candidate
        if not candidate.is_file():
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != package_dir:
            return None
        return resolved

    def get_item(self, album_id: str) -> LibraryItem:
        with self._lock:
            self._require_album_id(album_id)
            album_dir = self._album_directory(self.paths.pictures, album_id)
            pdf_album_dir = self._album_directory(self.paths.pdfs, album_id)

            manifest = None
            if album_dir.is_dir():
                try:
                    manifest = ChapterManifestStore(self.paths).load(album_id)
                except UnsupportedChapterManifestVersion as error:
                    # A newer application may own this directory.  Hiding it
                    # is safer than presenting destructive legacy actions.
                    raise LibraryNotFound(
                        "章节清单版本高于当前程序支持的版本"
                    ) from error
                except ChapterManifestError:
                    # Invalid or unreadable manifests never contribute a title
                    # or a path.  The remaining files are scanned only through
                    # the conservative legacy/unverified paths below.
                    manifest = None

            if manifest is not None:
                managed_images = self._managed_images(album_dir, manifest)
                pdf_directory, pdf_files = self._managed_pdfs(
                    pdf_album_dir,
                    manifest,
                )
                cbz_directory, cbz_files = self._managed_cbz(
                    pdf_album_dir,
                    manifest,
                )
                if managed_images or pdf_files or cbz_files:
                    return self._item_from_files(
                        album_id=album_id,
                        title=manifest.album_title,
                        layout=LibraryLayout.MANAGED,
                        chapter_count=len(manifest.chapters),
                        images=managed_images,
                        pdf_directory=pdf_directory,
                        pdf_files=pdf_files,
                        cbz_directory=cbz_directory,
                        cbz_files=cbz_files,
                        downloaded_at_utc=(
                            self._manifest_downloaded_at(manifest)
                            or self._directory_downloaded_at(
                                album_dir,
                                pdf_album_dir,
                            )
                        ),
                    )
                if (
                    manifest.chapters
                    and not self._list_images(album_dir)
                    and not self._list_pdf_files(pdf_album_dir)
                    and not self._list_package_files(pdf_album_dir, ".cbz")
                ):
                    # Decision 2.6: images were deleted while the chapter
                    # records were kept.  The album stays visible and
                    # manageable only when no unmanaged files drifted into
                    # its roots; drifted files keep the conservative
                    # legacy/unverified branches below.
                    return self._item_from_files(
                        album_id=album_id,
                        title=manifest.album_title,
                        layout=LibraryLayout.MANAGED,
                        chapter_count=len(manifest.chapters),
                        images=(),
                        pdf_directory=None,
                        pdf_files=(),
                        cbz_directory=None,
                        cbz_files=(),
                        downloaded_at_utc=(
                            self._manifest_downloaded_at(manifest)
                            or self._directory_downloaded_at(
                                album_dir,
                                pdf_album_dir,
                            )
                        ),
                    )
                # A valid manifest whose declared image layout has drifted is
                # not rebound to a guessed title path.  The remaining disk
                # facts continue through the conservative legacy/PDF-only
                # branches below.

            images = self._list_images(album_dir)
            if images:
                return self._item_from_files(
                    album_id=album_id,
                    title=None,
                    layout=LibraryLayout.LEGACY,
                    chapter_count=self._legacy_directory_count(album_dir),
                    images=images,
                    pdf_directory=None,
                    pdf_files=(),
                    cbz_directory=None,
                    cbz_files=(),
                    downloaded_at_utc=self._directory_downloaded_at(
                        album_dir,
                    ),
                )

            pdf_files = self._list_pdf_files(pdf_album_dir)
            cbz_files = self._list_package_files(
                pdf_album_dir, ".cbz"
            )
            if not pdf_files and not cbz_files:
                raise LibraryNotFound("未找到该漫画")
            return self._item_from_files(
                album_id=album_id,
                title=None,
                layout=LibraryLayout.UNVERIFIED,
                chapter_count=0,
                images=(),
                pdf_directory=pdf_album_dir if pdf_files else None,
                pdf_files=pdf_files,
                cbz_directory=pdf_album_dir if cbz_files else None,
                cbz_files=cbz_files,
                downloaded_at_utc=self._directory_downloaded_at(
                    pdf_album_dir,
                ),
            )

    @staticmethod
    def _item_from_files(
        *,
        album_id: str,
        title: str | None,
        layout: LibraryLayout,
        chapter_count: int,
        images,
        pdf_directory: Path | None,
        pdf_files,
        cbz_directory: Path | None,
        cbz_files,
        downloaded_at_utc: str | None = None,
    ) -> LibraryItem:
        try:
            image_size = sum(path.stat().st_size for path in images)
            pdf_size = sum(path.stat().st_size for path in pdf_files)
            cbz_size = sum(path.stat().st_size for path in cbz_files)
        except OSError as error:
            raise LibraryNotFound(
                "本地漫画文件已发生变化，请刷新后重试"
            ) from error
        return LibraryItem(
            album_id=album_id,
            title=title,
            layout=layout,
            chapter_count=chapter_count,
            image_count=len(images),
            image_size=image_size,
            preview_path=images[0] if images else None,
            pdf_directory=pdf_directory,
            pdf_size=pdf_size,
            cbz_directory=cbz_directory,
            cbz_size=cbz_size,
            downloaded_at_utc=downloaded_at_utc,
        )

    def get_preview(self, album_id: str) -> Path:
        with self._lock:
            preview = self.get_item(album_id).preview_path
            if preview is None:
                raise LibraryNotFound("没有可用的预览图")
            return preview

    def get_pdf_directory(self, album_id: str) -> Path:
        with self._lock:
            item = self.get_item(album_id)
            if item.pdf_directory is None:
                raise LibraryNotFound("PDF 储存文件夹不存在")
            return item.pdf_directory

    def rebuild_chapters(
        self,
        album_id: str,
        photo_ids,
        *,
        confirmed_formats: Mapping[str, str] | None = None,
    ) -> ChapterRebuildResult:
        """Rebuild selected managed chapters in their recorded formats.

        Unknown legacy formats must be supplied explicitly in
        ``confirmed_formats``.  Chapters are committed independently so one
        failure does not undo earlier successful chapters.
        """
        with self._lock:
            self._require_album_id(album_id)
            selected_ids = self._normalize_photo_ids(photo_ids)
            if not selected_ids:
                raise LibraryError("请先选择要重建的章节")
            choices = {
                str(photo_id): str(package_format)
                for photo_id, package_format in dict(
                    confirmed_formats or {}
                ).items()
            }
            try:
                manifest = ChapterManifestStore(self.paths).load(album_id)
            except ChapterManifestError as error:
                raise LibraryError(
                    "章节清单不可用，无法重建章节"
                ) from error
            if manifest is None:
                raise LibraryNotFound("没有可用的章节清单")

            album_dir = self._album_directory(self.paths.pictures, album_id)
            title_dir = self._safe_child_directory(
                album_dir,
                manifest.album_dir_name,
            )
            package_album_dir = self._album_directory(
                self.paths.pdfs,
                album_id,
            )
            by_id = {
                chapter.photo_id: chapter for chapter in manifest.chapters
            }
            succeeded = []
            failures = []
            working_manifest = manifest
            for photo_id in selected_ids:
                chapter = by_id.get(photo_id)
                if chapter is None:
                    failures.append(
                        ChapterOperationFailure(
                            photo_id=photo_id,
                            title="",
                            message="章节已不在当前清单中",
                        )
                    )
                    continue
                try:
                    outcome, working_manifest = self._rebuild_chapter(
                        album_id=album_id,
                        manifest=working_manifest,
                        chapter=chapter,
                        title_dir=title_dir,
                        package_album_dir=package_album_dir,
                        confirmed_format=choices.get(photo_id),
                    )
                except Exception as error:
                    message = str(error).strip() or "章节重建失败"
                    failures.append(
                        ChapterOperationFailure(
                            photo_id=photo_id,
                            title=chapter.title,
                            message=message,
                        )
                    )
                else:
                    succeeded.append(outcome)
                    by_id[photo_id] = next(
                        value
                        for value in working_manifest.chapters
                        if value.photo_id == photo_id
                    )
            return ChapterRebuildResult(
                album_id=album_id,
                succeeded=tuple(succeeded),
                failures=tuple(failures),
            )

    def plan_chapter_repairs(
        self,
        album_id: str,
        photo_ids,
        *,
        confirmed_formats: Mapping[str, str] | None = None,
    ) -> ChapterRepairPlan:
        """Build an offline, side-effect-free repair plan for selected chapters."""

        with self._lock:
            self._require_album_id(album_id)
            selected_ids = self._normalize_photo_ids(photo_ids)
            if not selected_ids:
                raise LibraryError("请先选择要修复的章节")
            choices = {
                str(photo_id): str(package_format)
                for photo_id, package_format in dict(
                    confirmed_formats or {}
                ).items()
            }
            snapshots = {
                snapshot.photo_id: snapshot
                for snapshot in self.check_chapters(album_id)
            }
            rebuild = []
            unchanged = []
            failures = []
            resolved_formats = []
            grouped = {"pdf": [], "cbz": [], "images": []}
            for photo_id in selected_ids:
                snapshot = snapshots.get(photo_id)
                if snapshot is None:
                    failures.append(
                        ChapterOperationFailure(
                            photo_id=photo_id,
                            title="",
                            message="章节已不在当前清单中",
                        )
                    )
                    continue
                if "check_error" in snapshot.problem_codes:
                    failures.append(
                        ChapterOperationFailure(
                            photo_id=photo_id,
                            title=snapshot.title,
                            message="章节本地路径无法安全检查",
                        )
                    )
                    continue
                package_format = snapshot.package_format
                if package_format is None:
                    package_format = (
                        snapshot.suggested_package_format
                        or choices.get(photo_id)
                    )
                    if package_format not in {"pdf", "cbz", "images"}:
                        failures.append(
                            ChapterOperationFailure(
                                photo_id=photo_id,
                                title=snapshot.title,
                                message=(
                                    "章节原打包格式未知，请先确认 "
                                    "PDF、CBZ 或仅图片"
                                ),
                            )
                        )
                        continue
                    resolved_formats.append((photo_id, package_format))

                if snapshot.image_status is ChapterImageStatus.COMPLETE:
                    if snapshot.package_format is None:
                        # Even an images-only choice must be persisted after
                        # explicit confirmation.
                        rebuild.append(photo_id)
                    elif snapshot.package_status in {
                        ChapterPackageStatus.MISSING,
                        ChapterPackageStatus.DAMAGED,
                    }:
                        rebuild.append(photo_id)
                    else:
                        unchanged.append(photo_id)
                    continue
                grouped[package_format].append(photo_id)

            batches = tuple(
                ChapterRepairBatch(
                    package_format=package_format,
                    photo_ids=tuple(grouped[package_format]),
                )
                for package_format in ("pdf", "cbz", "images")
                if grouped[package_format]
            )
            return ChapterRepairPlan(
                album_id=album_id,
                rebuild_photo_ids=tuple(rebuild),
                download_batches=batches,
                unchanged_photo_ids=tuple(unchanged),
                failures=tuple(failures),
                resolved_formats=tuple(resolved_formats),
            )

    def plan_legacy_migration(
        self,
        album_id: str,
        catalog: ChapterCatalogSnapshot,
    ) -> LegacyMigrationPlan:
        """Map a legacy image layout only when every local source is unique."""

        with self._lock:
            self._require_album_id(album_id)
            if (
                not isinstance(catalog, ChapterCatalogSnapshot)
                or catalog.album_id != album_id
                or not catalog.chapters
            ):
                raise LibraryError("远端章节目录无效")
            album_dir = self._album_directory(self.paths.pictures, album_id)
            if not album_dir.is_dir():
                raise LibraryNotFound("旧版图片目录不存在")
            self._reject_linked_tree(album_dir)
            if ChapterManifestStore(self.paths).load(album_id) is not None:
                raise LibraryError("该漫画已经是受管章节布局")
            try:
                children = tuple(album_dir.iterdir())
            except OSError as error:
                raise LibraryError("无法读取旧版图片目录") from error
            direct_images = self._list_direct_images(album_dir)
            directories = tuple(
                child for child in children if child.is_dir()
            )
            extras = tuple(
                child
                for child in children
                if child not in direct_images and child not in directories
            )
            if extras or (direct_images and directories):
                raise LibraryError("旧版目录包含无法唯一识别的额外内容")

            if direct_images:
                if len(catalog.chapters) != 1:
                    raise LibraryError("直接图片无法唯一对应多章目录")
                sources = (("", direct_images, catalog.chapters[0]),)
            else:
                if not directories:
                    raise LibraryNotFound("旧版目录中没有可迁移图片")
                remote_by_name = {}
                for chapter in catalog.chapters:
                    key = fix_windir_name(chapter.title).strip().casefold()
                    remote_by_name.setdefault(key, []).append(chapter)
                sources = []
                used = set()
                for directory in directories:
                    try:
                        children = tuple(directory.iterdir())
                    except OSError as error:
                        raise LibraryError("无法读取旧章节目录") from error
                    images = self._list_direct_images(directory)
                    if not images or len(images) != len(children):
                        raise LibraryError(
                            f"旧章节“{directory.name}”包含非图片或嵌套内容"
                        )
                    matches = remote_by_name.get(
                        directory.name.casefold(),
                        (),
                    )
                    if (
                        len(matches) != 1
                        or matches[0].photo_id in used
                    ):
                        raise LibraryError(
                            f"旧章节“{directory.name}”无法唯一对应远端章节"
                        )
                    used.add(matches[0].photo_id)
                    sources.append((directory.name, images, matches[0]))
                sources = tuple(sources)

            album_title = catalog.title or f"JM {album_id}"
            album_dir_name = self._migration_album_dir_name(
                album_title,
                album_id,
            )
            mappings = []
            for source_name, images, chapter in sources:
                target_dir_name = (
                    ""
                    if len(catalog.chapters) == 1
                    else f"第{chapter.index}章"
                )
                mappings.append(
                    LegacyChapterMapping(
                        photo_id=chapter.photo_id,
                        index=chapter.index,
                        title=chapter.title,
                        source_name=source_name,
                        target_dir_name=target_dir_name,
                        page_count=len(images),
                        image_format=self._migration_image_format(images),
                        package_format=self._infer_migration_package_format(
                            album_id,
                            album_dir_name,
                            target_dir_name,
                        ),
                    )
                )
            return LegacyMigrationPlan(
                album_id=album_id,
                album_title=album_title,
                album_dir_name=album_dir_name,
                direct_images=bool(direct_images),
                mappings=tuple(
                    sorted(mappings, key=lambda value: value.index)
                ),
            )

    def migrate_legacy_layout(
        self,
        plan: LegacyMigrationPlan,
    ) -> ChapterManifest:
        """Apply a previewed migration using one staged album transaction."""

        with self._lock:
            if not isinstance(plan, LegacyMigrationPlan):
                raise LibraryError("旧版迁移方案无效")
            self._require_album_id(plan.album_id)
            album_dir = self._album_directory(
                self.paths.pictures,
                plan.album_id,
            )
            self._validate_migration_sources(album_dir, plan)
            staged = album_dir.with_name(
                f".{plan.album_id}.{uuid.uuid4().hex}.migrate"
            )
            try:
                os.replace(album_dir, staged)
                album_dir.mkdir()
                title_dir = album_dir / plan.album_dir_name
                if plan.direct_images or any(
                    value.target_dir_name for value in plan.mappings
                ):
                    title_dir.mkdir()
                for mapping in plan.mappings:
                    target = (
                        title_dir
                        if not mapping.target_dir_name
                        else title_dir / mapping.target_dir_name
                    )
                    if mapping.target_dir_name:
                        shutil.copytree(staged / mapping.source_name, target)
                    elif plan.direct_images:
                        for image in self._list_direct_images(staged):
                            shutil.copy2(image, target / image.name)
                    else:
                        shutil.copytree(staged / mapping.source_name, target)
                manifest = ChapterManifest(
                    version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                    album_id=plan.album_id,
                    album_title=plan.album_title,
                    album_dir_name=plan.album_dir_name,
                    chapters=tuple(
                        ChapterManifestEntry(
                            photo_id=value.photo_id,
                            index=value.index,
                            title=value.title,
                            dir_name=value.target_dir_name,
                            page_count=value.page_count,
                            image_format=value.image_format,
                            downloaded_at_utc=None,
                            package_format=value.package_format,
                        )
                        for value in plan.mappings
                    ),
                )
                ChapterManifestStore(self.paths).replace_exact(manifest)
            except Exception as error:
                rollback_errors = []
                try:
                    if album_dir.exists():
                        shutil.rmtree(album_dir)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
                if staged.exists():
                    try:
                        os.replace(staged, album_dir)
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    raise LibraryError(
                        "旧版迁移失败，且无法完整回滚："
                        + "; ".join(rollback_errors)
                    ) from error
                raise LibraryError(f"旧版迁移失败：{error}") from error
            try:
                shutil.rmtree(staged)
            except OSError as error:
                raise LibraryError(
                    f"迁移已完成，但临时目录清理失败：{error}"
                ) from error
            return manifest

    def _validate_migration_sources(
        self,
        album_dir: Path,
        plan: LegacyMigrationPlan,
    ) -> None:
        self._reject_linked_tree(album_dir)
        if ChapterManifestStore(self.paths).load(plan.album_id) is not None:
            raise LibraryError("旧版目录已发生变化，请重新识别")
        expected_names = {value.source_name for value in plan.mappings}
        if plan.direct_images:
            images = self._list_direct_images(album_dir)
            if len(plan.mappings) != 1:
                raise LibraryError("旧版迁移方案无效")
            mapping = plan.mappings[0]
            if (
                len(images) != mapping.page_count
                or self._migration_image_format(images)
                != mapping.image_format
                or len(tuple(album_dir.iterdir())) != len(images)
            ):
                raise LibraryError("旧版目录已发生变化，请重新识别")
            return
        directories = {
            child.name: child
            for child in album_dir.iterdir()
            if child.is_dir()
        }
        if set(directories) != expected_names:
            raise LibraryError("旧版目录已发生变化，请重新识别")
        for mapping in plan.mappings:
            images = self._list_direct_images(directories[mapping.source_name])
            if (
                len(images) != mapping.page_count
                or self._migration_image_format(images)
                != mapping.image_format
                or len(tuple(directories[mapping.source_name].iterdir()))
                != len(images)
            ):
                raise LibraryError("旧版目录已发生变化，请重新识别")

    def _migration_album_dir_name(self, title: str, album_id: str) -> str:
        pictures_root = str(self.paths.pictures.resolve())
        package_root = str(self.paths.pdfs.resolve())
        image_overhead = (
            len(pictures_root)
            + 1
            + len(album_id)
            + len("\\第999999章\\00000.jpg")
        )
        single_package_overhead = (
            len(package_root)
            + 1
            + len(album_id)
            + len("\\\\.pdf")
        )
        budget = min(
            100,
            _MANAGED_PATH_LIMIT - image_overhead,
            (_MANAGED_PATH_LIMIT - single_package_overhead) // 2,
        )
        if budget < 1:
            raise LibraryError("当前存储路径过长，无法安全迁移旧版章节")
        value = fix_windir_name(title).strip(" .")[:budget].strip(" .")
        if not value:
            value = album_id[:budget].strip(" .")
        if not value:
            raise LibraryError("无法生成安全的漫画目录名")
        if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            value = f"_{value}"[:budget].strip(" .")
        ChapterManifestStore._validate_component(value)
        return value

    def _migration_image_format(self, images: tuple[Path, ...]) -> str:
        if not images or not all(
            self._is_valid_image_file(path) for path in images
        ):
            raise LibraryError("旧章节包含损坏图片")
        suffixes = {path.suffix.lower() for path in images}
        if suffixes <= {".jpg", ".jpeg"}:
            return "jpg"
        if suffixes == {".png"}:
            return "png"
        raise LibraryError("旧章节图片格式不一致")

    def _infer_migration_package_format(
        self,
        album_id: str,
        album_dir_name: str,
        target_dir_name: str,
    ) -> str | None:
        package_album_dir = self._album_directory(self.paths.pdfs, album_id)
        package_dir = self._safe_child_directory(
            package_album_dir,
            album_dir_name,
        )
        if package_dir is None:
            return None
        name = target_dir_name or album_dir_name
        pdf = (package_dir / f"{name}.pdf").is_file()
        cbz = (package_dir / f"{name}.cbz").is_file()
        if pdf == cbz:
            return None
        return "pdf" if pdf else "cbz"

    def _rebuild_chapter(
        self,
        *,
        album_id: str,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
        title_dir: Path | None,
        package_album_dir: Path,
        confirmed_format: str | None,
    ) -> tuple[ChapterRebuildOutcome, ChapterManifest]:
        snapshot = self._check_chapter(
            album_id,
            manifest,
            chapter,
            title_dir,
            self._safe_child_directory(
                package_album_dir,
                manifest.album_dir_name,
            ),
        )
        if snapshot.image_status is not ChapterImageStatus.COMPLETE:
            raise LibraryError("章节图片不完整，请先重新下载")

        package_format = chapter.package_format
        format_was_unknown = package_format is None
        if format_was_unknown:
            if confirmed_format not in {"pdf", "cbz", "images"}:
                raise LibraryError(
                    "章节原打包格式未知，请先确认 PDF、CBZ 或仅图片"
                )
            package_format = confirmed_format

        updated_manifest = manifest
        if format_was_unknown:
            updated_manifest = self._manifest_with_chapter_format(
                manifest,
                chapter.photo_id,
                package_format,
            )

        if package_format == "images":
            if format_was_unknown:
                try:
                    ChapterManifestStore(self.paths).replace_exact(
                        updated_manifest
                    )
                except ChapterManifestError as error:
                    raise LibraryError(
                        f"无法保存章节原格式：{error}"
                    ) from error
            return (
                ChapterRebuildOutcome(
                    photo_id=chapter.photo_id,
                    index=chapter.index,
                    title=chapter.title,
                    package_format=package_format,
                    output_path=None,
                ),
                updated_manifest,
            )

        image_directory = snapshot.image_directory
        if image_directory is None:
            raise LibraryError("章节图片目录不可用")
        package_dir, created_directories = self._prepare_package_directory(
            package_album_dir,
            manifest.album_dir_name,
        )
        target = self._validated_package_target(
            package_dir,
            manifest,
            chapter,
            package_format,
        )
        backup = None
        if target.exists():
            backup = target.with_name(
                f".{target.name}.{uuid.uuid4().hex}.rebuild"
            )
            try:
                os.replace(target, backup)
            except OSError as error:
                self._remove_empty_directories(created_directories)
                raise LibraryError(
                    f"无法暂存现有打包产物：{error}"
                ) from error

        published = False
        try:
            if package_format == "pdf":
                result = chapter_to_pdf(image_directory, target)
            else:
                result = chapter_to_cbz(image_directory, target)
            if not result:
                raise LibraryError("章节打包未生成产物")
            published = True
            if format_was_unknown:
                ChapterManifestStore(self.paths).replace_exact(
                    updated_manifest
                )
        except Exception as error:
            rollback_errors = self._rollback_rebuild_target(
                target=target,
                backup=backup,
                published=published,
            )
            self._remove_empty_directories(created_directories)
            if rollback_errors:
                raise LibraryError(
                    "章节重建失败，且无法完整回滚："
                    + "; ".join(rollback_errors)
                ) from error
            if isinstance(error, LibraryError):
                raise
            raise LibraryError(f"章节重建失败：{error}") from error

        warning = None
        if backup is not None:
            try:
                backup.unlink()
            except OSError as error:
                warning = f"章节已重建，但旧产物备份清理失败：{error}"
        return (
            ChapterRebuildOutcome(
                photo_id=chapter.photo_id,
                index=chapter.index,
                title=chapter.title,
                package_format=package_format,
                output_path=target,
                warning=warning,
            ),
            updated_manifest,
        )

    @staticmethod
    def _normalize_photo_ids(photo_ids) -> tuple[str, ...]:
        values = (photo_ids,) if isinstance(photo_ids, str) else photo_ids
        normalized = []
        seen = set()
        try:
            iterator = iter(values)
        except TypeError as error:
            raise LibraryError("章节选择无效") from error
        for value in iterator:
            photo_id = str(value)
            if photo_id in seen:
                continue
            seen.add(photo_id)
            normalized.append(photo_id)
        return tuple(normalized)

    @staticmethod
    def _manifest_with_chapter_format(
        manifest: ChapterManifest,
        photo_id: str,
        package_format: str,
    ) -> ChapterManifest:
        return replace(
            manifest,
            chapters=tuple(
                replace(chapter, package_format=package_format)
                if chapter.photo_id == photo_id
                else chapter
                for chapter in manifest.chapters
            ),
        )

    @staticmethod
    def _prepare_package_directory(
        package_album_dir: Path,
        album_dir_name: str,
    ) -> tuple[Path, tuple[Path, ...]]:
        created = []
        try:
            if not package_album_dir.exists():
                package_album_dir.mkdir()
                created.append(package_album_dir)
            if (
                is_linked_directory(package_album_dir)
                or not package_album_dir.is_dir()
            ):
                raise LibraryError("打包产物目录不可用")
            resolved_album_dir = package_album_dir.resolve(strict=True)
            package_dir = package_album_dir / album_dir_name
            if not package_dir.exists():
                package_dir.mkdir()
                created.append(package_dir)
            if is_linked_directory(package_dir) or not package_dir.is_dir():
                raise LibraryError("打包产物目录不可用")
            resolved_package_dir = package_dir.resolve(strict=True)
            if resolved_package_dir.parent != resolved_album_dir:
                raise LibraryError("打包产物目录超出受管范围")
            return resolved_package_dir, tuple(created)
        except LibraryError:
            LibraryService._remove_empty_directories(tuple(created))
            raise
        except OSError as error:
            LibraryService._remove_empty_directories(tuple(created))
            raise LibraryError(f"无法准备打包产物目录：{error}") from error

    @staticmethod
    def _validated_package_target(
        package_dir: Path,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
        package_format: str,
    ) -> Path:
        name = chapter.dir_name or manifest.album_dir_name
        target = package_dir / f"{name}.{package_format}"
        if is_linked_directory(target):
            raise LibraryError("打包产物路径不能是链接")
        if target.exists():
            if not target.is_file():
                raise LibraryError("打包产物路径无效")
            try:
                resolved = target.resolve(strict=True)
            except OSError as error:
                raise LibraryError("打包产物路径无法安全解析") from error
            if resolved.parent != package_dir:
                raise LibraryError("打包产物路径超出受管范围")
            return resolved
        return target

    @staticmethod
    def _rollback_rebuild_target(
        *,
        target: Path,
        backup: Path | None,
        published: bool,
    ) -> list[str]:
        errors = []
        if published or target.exists():
            try:
                target.unlink(missing_ok=True)
            except OSError as error:
                errors.append(str(error))
        if backup is not None and backup.exists():
            try:
                os.replace(backup, target)
            except OSError as error:
                errors.append(str(error))
        return errors

    @staticmethod
    def _remove_empty_directories(directories) -> None:
        for directory in reversed(tuple(directories)):
            if directory is None:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass

    def delete_images(self, album_id: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            item = self.get_item(album_id)
            if not item.has_images:
                raise LibraryNotFound("图片目录不存在")
            album_dir = self._album_directory(self.paths.pictures, album_id)
            self._reject_linked_tree(album_dir)

            if item.layout is LibraryLayout.MANAGED:
                try:
                    manifest = ChapterManifestStore(self.paths).load(album_id)
                except ChapterManifestError as error:
                    raise LibraryError(
                        "章节清单已发生变化，未删除图片"
                    ) from error
                if manifest is None:
                    raise LibraryError("章节清单已发生变化，未删除图片")
                self._delete_managed_images(album_dir, manifest)
                return

            self._delete_directory(album_dir, label="图片")

    def delete_chapter(
        self,
        album_id: str,
        photo_id: str,
        kind: str,
        *,
        expected: LibraryChapterSnapshot | None = None,
    ) -> ChapterDeleteResult:
        """Delete one manifest chapter through a staged local transaction."""

        with self._lock:
            self._require_album_id(album_id)
            photo_id = str(photo_id)
            kind = str(kind)
            if kind not in {"images", "package", "all"}:
                raise LibraryError("不支持的章节删除类型")
            try:
                store = ChapterManifestStore(self.paths)
                manifest = store.load(album_id)
            except ChapterManifestError as error:
                raise LibraryError(
                    "章节清单不可用，未删除章节内容"
                ) from error
            if manifest is None:
                raise LibraryNotFound("没有可用的章节清单")
            chapter = next(
                (
                    value
                    for value in manifest.chapters
                    if value.photo_id == photo_id
                ),
                None,
            )
            if chapter is None:
                raise LibraryNotFound("章节已不在当前清单中")

            album_dir = self._album_directory(
                self.paths.pictures,
                album_id,
            )
            package_album_dir = self._album_directory(
                self.paths.pdfs,
                album_id,
            )
            title_dir, image_directory = self._chapter_delete_image_path(
                album_dir,
                manifest,
                chapter,
            )
            package_dir, package_paths = self._chapter_delete_package_paths(
                package_album_dir,
                manifest,
                chapter,
            )
            current = self._check_chapter(
                album_id,
                manifest,
                chapter,
                title_dir,
                package_dir,
            )
            if expected is not None:
                if (
                    not isinstance(expected, LibraryChapterSnapshot)
                    or expected.album_id != album_id
                    or expected.photo_id != photo_id
                    or expected != current
                ):
                    raise LibraryError(
                        "章节内容已发生变化，请重新检查后再删除"
                    )

            images = (
                self._chapter_delete_images(image_directory)
                if image_directory is not None
                else ()
            )
            if kind == "images":
                if not images:
                    raise LibraryNotFound("章节图片不存在")
                targets = list(images)
            elif kind == "package":
                if chapter.package_format == "images":
                    raise LibraryError("仅图片章节没有打包产物")
                if not package_paths:
                    raise LibraryNotFound("章节打包产物不存在")
                targets = list(package_paths)
            else:
                targets = [*images, *package_paths]

            remaining = tuple(
                value
                for value in manifest.chapters
                if value.photo_id != photo_id
            )
            remove_album = kind == "all" and not remaining
            if remove_album:
                manifest_path = store._manifest_path(album_id)
                if (
                    manifest_path.is_symlink()
                    or not manifest_path.is_file()
                ):
                    raise LibraryError("章节清单已发生变化，未删除章节")
                targets.append(manifest_path)

            staged = self._stage_chapter_targets(
                targets,
                label=f"chapter-{kind}",
            )
            try:
                if kind == "all" and remaining:
                    store.replace_exact(
                        replace(manifest, chapters=remaining)
                    )
                elif kind != "all":
                    # Rewriting the unchanged manifest makes the file and
                    # manifest portions one rollback boundary.
                    store.replace_exact(manifest)
            except (OSError, ChapterManifestError) as error:
                rollback_errors = self._rollback_staged(staged)
                if rollback_errors:
                    raise LibraryError(
                        "删除章节失败，且无法完整回滚："
                        + "; ".join(rollback_errors)
                    ) from error
                raise LibraryError(f"删除章节失败：{error}") from error

            cleanup_errors = self._discard_staged_targets(staged)
            if remove_album:
                self._remove_empty_directories(
                    (
                        album_dir,
                        title_dir,
                        image_directory,
                        package_album_dir,
                        package_dir,
                    )
                )
            elif image_directory is not None:
                self._remove_empty_directories((image_directory,))
            if cleanup_errors:
                raise LibraryError(
                    "章节内容已移出本地库，但临时文件清理失败："
                    + "; ".join(cleanup_errors)
                )
            return ChapterDeleteResult(
                album_id=album_id,
                photo_id=photo_id,
                kind=kind,
                deleted_image_count=len(images)
                if kind in {"images", "all"}
                else 0,
                deleted_package_count=len(package_paths)
                if kind in {"package", "all"}
                else 0,
                album_removed=remove_album,
            )

    def _chapter_delete_image_path(
        self,
        album_dir: Path,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
    ) -> tuple[Path | None, Path | None]:
        raw_title = album_dir / manifest.album_dir_name
        if is_linked_directory(raw_title):
            raise LibraryError("章节图片目录不能是链接")
        if raw_title.exists() and not raw_title.is_dir():
            raise LibraryError("章节图片目录结构无效")
        title_dir = self._safe_child_directory(
            album_dir,
            manifest.album_dir_name,
        )
        if title_dir is None:
            return None, None
        raw_chapter = (
            title_dir
            if not chapter.dir_name
            else title_dir / chapter.dir_name
        )
        if is_linked_directory(raw_chapter):
            raise LibraryError("章节图片目录不能是链接")
        if raw_chapter.exists() and not raw_chapter.is_dir():
            raise LibraryError("章节图片目录结构无效")
        return (
            title_dir,
            self._chapter_image_directory(title_dir, chapter),
        )

    def _chapter_delete_package_paths(
        self,
        package_album_dir: Path,
        manifest: ChapterManifest,
        chapter: ChapterManifestEntry,
    ) -> tuple[Path | None, tuple[Path, ...]]:
        raw_package_dir = package_album_dir / manifest.album_dir_name
        if is_linked_directory(raw_package_dir):
            raise LibraryError("章节打包目录不能是链接")
        if raw_package_dir.exists() and not raw_package_dir.is_dir():
            raise LibraryError("章节打包目录结构无效")
        package_dir = self._safe_child_directory(
            package_album_dir,
            manifest.album_dir_name,
        )
        if package_dir is None:
            return None, ()
        name = chapter.dir_name or manifest.album_dir_name
        paths = []
        for suffix in ("pdf", "cbz"):
            candidate = package_dir / f"{name}.{suffix}"
            if is_linked_directory(candidate):
                raise LibraryError("章节打包产物不能是链接")
            if not candidate.exists():
                continue
            if not candidate.is_file():
                raise LibraryError("章节打包产物路径无效")
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise LibraryError(
                    "章节打包产物无法安全解析"
                ) from error
            if resolved.parent != package_dir:
                raise LibraryError("章节打包产物超出受管范围")
            paths.append(resolved)
        return package_dir, tuple(paths)

    def _chapter_delete_images(
        self,
        image_directory: Path,
    ) -> tuple[Path, ...]:
        try:
            children = tuple(image_directory.iterdir())
        except OSError as error:
            raise LibraryError(
                "无法安全检查章节图片目录"
            ) from error
        if any(is_linked_directory(path) for path in children):
            raise LibraryError("章节图片目录包含链接，未执行删除")
        return self._list_direct_images(image_directory)

    @staticmethod
    def _stage_chapter_targets(
        targets,
        *,
        label: str,
    ) -> list[tuple[Path, Path]]:
        staged = []
        token = uuid.uuid4().hex
        try:
            for original in targets:
                original = Path(original)
                staged_path = original.with_name(
                    f".{original.name}.{token}.{label}.delete"
                )
                os.replace(original, staged_path)
                staged.append((staged_path, original))
        except OSError as error:
            rollback_errors = LibraryService._rollback_staged(staged)
            if rollback_errors:
                raise LibraryError(
                    "删除章节失败，且无法完整回滚："
                    + "; ".join(rollback_errors)
                ) from error
            raise LibraryError(f"删除章节失败：{error}") from error
        return staged

    @staticmethod
    def _discard_staged_targets(
        staged: list[tuple[Path, Path]],
    ) -> list[str]:
        errors = []
        for staged_path, _original in staged:
            try:
                if staged_path.is_dir():
                    shutil.rmtree(staged_path)
                else:
                    staged_path.unlink()
            except OSError as error:
                errors.append(str(error))
        return errors

    def delete_pdf(self, album_id: str) -> None:
        self.delete_packaged_artifacts(album_id)

    def delete_packaged_artifacts(self, album_id: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            item = self.get_item(album_id)
            if not item.has_pdf and not item.has_cbz:
                raise LibraryNotFound("打包产物文件夹不存在")
            pdf_album_dir = self._album_directory(self.paths.pdfs, album_id)
            self._reject_linked_tree(pdf_album_dir)
            self._delete_directory(pdf_album_dir, label="打包产物")

    def delete_all(self, album_id: str) -> None:
        with self._lock:
            self._require_album_id(album_id)
            self.get_item(album_id)
            album_dir = self._album_directory(self.paths.pictures, album_id)
            pdf_album_dir = self._album_directory(self.paths.pdfs, album_id)
            targets = [
                path for path in (album_dir, pdf_album_dir) if path.is_dir()
            ]
            for path in targets:
                self._reject_linked_tree(path)
            token = uuid.uuid4().hex
            staged = []
            try:
                for original in targets:
                    staged_path = original.with_name(
                        f".{album_id}.{token}.{original.parent.name}.delete"
                    )
                    os.replace(original, staged_path)
                    staged.append((staged_path, original))
            except OSError as error:
                rollback_errors = self._rollback_staged(staged)
                if rollback_errors:
                    details = "; ".join(rollback_errors)
                    raise LibraryError(
                        f"删除漫画失败，且无法完整回滚：{details}"
                    ) from error
                raise LibraryError(f"删除漫画失败：{error}") from error

            cleanup_errors = []
            for staged_path, _original_path in staged:
                try:
                    shutil.rmtree(staged_path)
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
            item = self.get_item(album_id)
            if kind == "images":
                if not item.has_images:
                    raise LibraryNotFound("图片目录不存在")
                target = self._album_directory(self.paths.pictures, album_id)
            elif kind == "pdf":
                if item.pdf_directory is None:
                    raise LibraryNotFound("PDF 储存文件夹不存在")
                target = item.pdf_directory
            elif kind in {"cbz", "package"}:
                target = item.cbz_directory or item.pdf_directory
                if target is None:
                    raise LibraryNotFound("打包产物文件夹不存在")
            else:
                raise LibraryError("不支持的打开类型")

            if not hasattr(os, "startfile"):
                raise LibraryError("当前系统不支持从程序打开文件")
        try:
            os.startfile(target)
        except OSError as error:
            raise LibraryError(f"打开失败：{error}") from error

    def _safe_album_ids(self, root: Path) -> set[str]:
        resolved_root = self._require_real_root(root)
        try:
            candidates = tuple(root.iterdir())
        except OSError as error:
            raise LibraryError(f"无法读取本地库目录：{error}") from error

        result = set()
        for candidate in candidates:
            if not self._valid_album_id(candidate.name):
                continue
            if is_linked_directory(candidate) or not candidate.is_dir():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.parent == resolved_root:
                result.add(candidate.name)
        return result

    def _album_directory(self, root: Path, album_id: str) -> Path:
        resolved_root = self._require_real_root(root)
        candidate = root / album_id
        if is_linked_directory(candidate):
            raise LibraryNotFound("不支持链接形式的漫画目录")
        if candidate.exists():
            if not candidate.is_dir():
                raise LibraryNotFound("漫画目录结构无效")
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise LibraryNotFound("漫画目录无法安全解析") from error
        else:
            resolved = resolved_root / album_id
        if resolved.parent != resolved_root:
            raise LibraryNotFound("漫画目录不在受管目录中")
        return resolved

    @staticmethod
    def _require_real_root(root: Path) -> Path:
        if is_linked_directory(root) or not root.is_dir():
            raise LibraryNotFound("本地库根目录不可用")
        try:
            return root.resolve(strict=True)
        except OSError as error:
            raise LibraryNotFound("本地库根目录无法安全解析") from error

    def _managed_images(
        self,
        album_dir: Path,
        manifest: ChapterManifest,
    ) -> tuple[Path, ...]:
        title_dir = self._safe_child_directory(
            album_dir,
            manifest.album_dir_name,
        )
        if title_dir is None:
            return ()
        images = {}
        for chapter in sorted(
            manifest.chapters,
            key=lambda value: value.index,
        ):
            chapter_dir = title_dir
            if chapter.dir_name:
                chapter_dir = self._safe_child_directory(
                    title_dir,
                    chapter.dir_name,
                )
                if chapter_dir is None:
                    continue
            chapter_images = self._list_direct_images(chapter_dir)
            for image in chapter_images:
                images[image] = None
        return tuple(images)

    def _managed_pdfs(
        self,
        pdf_album_dir: Path,
        manifest: ChapterManifest,
    ) -> tuple[Path | None, tuple[Path, ...]]:
        target = self._safe_child_directory(
            pdf_album_dir,
            manifest.album_dir_name,
        )
        if target is None:
            return None, ()
        pdf_files = self._list_direct_pdfs(target)
        if not pdf_files:
            return None, ()
        return target, pdf_files

    def _managed_cbz(
        self,
        pdf_album_dir: Path,
        manifest: ChapterManifest,
    ) -> tuple[Path | None, tuple[Path, ...]]:
        target = self._safe_child_directory(
            pdf_album_dir,
            manifest.album_dir_name,
        )
        if target is None:
            return None, ()
        cbz_files = self._list_direct_packages(target, ".cbz")
        if not cbz_files:
            return None, ()
        return target, cbz_files

    @staticmethod
    def _manifest_downloaded_at(
        manifest: ChapterManifest,
    ) -> str | None:
        values = [
            chapter.downloaded_at_utc
            for chapter in manifest.chapters
            if chapter.downloaded_at_utc is not None
        ]
        return (
            max(
                values,
                key=lambda value: datetime.fromisoformat(
                    value[:-1] + "+00:00"
                ),
            )
            if values
            else None
        )

    @staticmethod
    def _directory_downloaded_at(*directories: Path) -> str | None:
        timestamps = []
        for directory in directories:
            if is_linked_directory(directory) or not directory.is_dir():
                continue
            try:
                timestamps.append(directory.stat().st_mtime)
            except OSError:
                continue
        if not timestamps:
            return None
        return (
            datetime.fromtimestamp(max(timestamps), timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _safe_child_directory(parent: Path, name: str) -> Path | None:
        if not parent.is_dir():
            return None
        candidate = parent / name
        if is_linked_directory(candidate) or not candidate.is_dir():
            return None
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != resolved_parent:
            return None
        return resolved

    @staticmethod
    def _list_direct_images(directory: Path) -> tuple[Path, ...]:
        try:
            candidates = tuple(directory.iterdir())
        except OSError as error:
            raise LibraryNotFound(
                "本地漫画文件已发生变化，请刷新后重试"
            ) from error
        images = []
        for candidate in candidates:
            if (
                candidate.suffix.lower() not in IMAGE_EXTENSIONS
                or PART_FILE_MARKER in candidate.name
                or is_linked_directory(candidate)
                or not candidate.is_file()
            ):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.parent == directory:
                images.append(resolved)
        images.sort(key=lambda path: natural_key(path.name))
        return tuple(images)

    @staticmethod
    def _images_match_format(
        images: tuple[Path, ...],
        image_format: str | None,
    ) -> bool:
        if image_format is None:
            return True
        if image_format == "jpg":
            allowed = {".jpg", ".jpeg"}
        elif image_format == "png":
            allowed = {".png"}
        else:
            return False
        return all(path.suffix.lower() in allowed for path in images)

    @staticmethod
    def _is_valid_image_file(path: Path) -> bool:
        try:
            if path.stat().st_size <= 0:
                return False
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, ValueError, UnidentifiedImageError):
            return False

    @staticmethod
    def _list_direct_pdfs(directory: Path) -> tuple[Path, ...]:
        return LibraryService._list_direct_packages(directory, ".pdf")

    @staticmethod
    def _list_direct_packages(
        directory: Path,
        suffix: str,
    ) -> tuple[Path, ...]:
        try:
            candidates = tuple(directory.iterdir())
        except OSError as error:
            raise LibraryNotFound(
                "本地 PDF 文件已发生变化，请刷新后重试"
            ) from error
        package_files = []
        for candidate in candidates:
            if (
                candidate.suffix.lower() != suffix
                or PART_FILE_MARKER in candidate.name
                or is_linked_directory(candidate)
                or not candidate.is_file()
            ):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.parent == directory:
                package_files.append(resolved)
        package_files.sort(key=lambda path: natural_key(path.name))
        return tuple(package_files)

    def _list_pdf_files(self, album_dir: Path) -> tuple[Path, ...]:
        return self._list_package_files(album_dir, ".pdf")

    def _list_package_files(
        self,
        album_dir: Path,
        suffix: str,
    ) -> tuple[Path, ...]:
        if not album_dir.is_dir() or is_linked_directory(album_dir):
            return ()
        try:
            resolved_album = album_dir.resolve(strict=True)
        except OSError:
            return ()

        package_files = []
        walk_errors = []
        for root, directories, filenames in os.walk(
            album_dir,
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

            for name in filenames:
                candidate = root_path / name
                if (
                    candidate.suffix.lower() != suffix
                    or PART_FILE_MARKER in candidate.name
                    or is_linked_directory(candidate)
                    or not candidate.is_file()
                ):
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if resolved.is_relative_to(resolved_album):
                    package_files.append(resolved)
        if walk_errors:
            raise LibraryNotFound(
                "本地 PDF 文件已发生变化，请刷新后重试"
            ) from walk_errors[0]
        package_files.sort(
            key=lambda path: tuple(
                natural_key(part)
                for part in path.relative_to(resolved_album).parts
            )
        )
        return tuple(package_files)

    def _list_images(self, album_dir: Path) -> list[Path]:
        if not album_dir.is_dir():
            return []
        try:
            return find_album_images(album_dir)
        except OSError as error:
            raise LibraryNotFound(
                "本地漫画文件已发生变化，请刷新后重试"
            ) from error

    @staticmethod
    def _legacy_directory_count(album_dir: Path) -> int:
        try:
            return sum(
                candidate.is_dir() and not is_linked_directory(candidate)
                for candidate in album_dir.iterdir()
            )
        except OSError as error:
            raise LibraryNotFound(
                "本地漫画文件已发生变化，请刷新后重试"
            ) from error

    @staticmethod
    def _reject_linked_tree(root: Path) -> None:
        if is_linked_directory(root) or not root.is_dir():
            raise LibraryError("拒绝操作链接形式的本地漫画目录")
        walk_errors = []
        for current, directories, filenames in os.walk(
            root,
            followlinks=False,
            onerror=walk_errors.append,
        ):
            current_path = Path(current)
            for name in (*directories, *filenames):
                if is_linked_directory(current_path / name):
                    raise LibraryError("本地漫画目录包含链接，未执行删除")
        if walk_errors:
            raise LibraryError(
                f"无法安全检查本地漫画目录：{walk_errors[0]}"
            )

    def _delete_managed_images(
        self,
        album_dir: Path,
        manifest: ChapterManifest,
    ) -> None:
        token = uuid.uuid4().hex
        staged = album_dir.with_name(
            f".{album_dir.name}.{token}.images.delete"
        )
        try:
            os.replace(album_dir, staged)
        except OSError as error:
            raise LibraryError(f"删除图片失败：{error}") from error

        try:
            album_dir.mkdir()
            ChapterManifestStore(self.paths).replace_exact(manifest)
        except (OSError, ChapterManifestError) as error:
            rollback_errors = []
            try:
                if album_dir.exists():
                    shutil.rmtree(album_dir)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
            if not album_dir.exists():
                try:
                    os.replace(staged, album_dir)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise LibraryError(
                    "删除图片失败，且无法完整回滚："
                    + "; ".join(rollback_errors)
                ) from error
            raise LibraryError(f"删除图片失败：{error}") from error

        try:
            shutil.rmtree(staged)
        except OSError as error:
            raise LibraryError(
                f"图片已移出本地库，但临时文件清理失败：{error}"
            ) from error

    def _delete_directory(self, directory: Path, *, label: str) -> None:
        token = uuid.uuid4().hex
        staged = directory.with_name(
            f".{directory.name}.{token}.{label}.delete"
        )
        try:
            os.replace(directory, staged)
        except OSError as error:
            raise LibraryError(f"删除{label}失败：{error}") from error
        try:
            shutil.rmtree(staged)
        except OSError as error:
            raise LibraryError(
                f"{label}已移出本地库，但临时文件清理失败：{error}"
            ) from error

    @staticmethod
    def _rollback_staged(
        staged: list[tuple[Path, Path]],
    ) -> list[str]:
        errors = []
        for staged_path, original_path in reversed(staged):
            try:
                os.replace(staged_path, original_path)
            except OSError as error:
                errors.append(str(error))
        return errors

    @staticmethod
    def _valid_album_id(album_id: str) -> bool:
        return (
            isinstance(album_id, str)
            and bool(album_id)
            and album_id.isascii()
            and album_id.isdigit()
        )

    def _require_album_id(self, album_id: str) -> None:
        if not self._valid_album_id(album_id):
            raise LibraryNotFound("漫画编号无效")
