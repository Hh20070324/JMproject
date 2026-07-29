import asyncio
from dataclasses import dataclass
from pathlib import Path
import threading

from PIL import Image, UnidentifiedImageError

from .library import (
    ChapterManifestError,
    ChapterManifestStore,
    LibraryService,
)
from .models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderErrorKind,
    ReaderPageSnapshot,
    ReaderPageState,
)
from .pdf import is_linked_directory
from .reader import (
    MAX_READER_CHAPTER_PAGES,
    MAX_READER_IMAGE_PIXELS,
    MAX_READER_IMAGE_SIDE,
    ReaderServiceError,
)
from .settings import AppPaths, DEFAULT_PATHS
from .tasks import InvalidAlbumId, normalize_album_id


@dataclass(frozen=True, slots=True)
class _LocalPage:
    path: Path
    signature: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _LocalChapter:
    snapshot: ReaderChapterSnapshot
    pages: tuple[_LocalPage, ...]


@dataclass(frozen=True, slots=True)
class _LocalAlbum:
    catalog: ChapterCatalogSnapshot
    chapters: dict[str, _LocalChapter]


class LocalReaderService:
    """Read complete managed image chapters without network or temp copies."""

    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        if not isinstance(paths, AppPaths):
            raise TypeError("paths must be AppPaths")
        self.paths = paths
        self._manifest_store = ChapterManifestStore(
            paths,
            ensure_directories=False,
        )
        self._albums: dict[str, _LocalAlbum] = {}
        self._lock = threading.RLock()
        self._closed = False

    async def start(self) -> None:
        self._ensure_open()

    async def close(self) -> bool:
        with self._lock:
            self._closed = True
            self._albums.clear()
        return True

    async def fetch_catalog(
        self,
        album_id: str,
    ) -> ChapterCatalogSnapshot:
        self._ensure_open()
        return await asyncio.to_thread(self._fetch_catalog_sync, album_id)

    async def load_chapter(
        self,
        catalog: ChapterCatalogSnapshot,
        photo_id: str,
    ) -> tuple[
        ReaderChapterSnapshot,
        tuple[ReaderPageSnapshot, ...],
    ]:
        self._ensure_open()
        if not isinstance(catalog, ChapterCatalogSnapshot):
            raise TypeError("catalog must be ChapterCatalogSnapshot")
        normalized_photo_id = self._normalize_id(photo_id, "章节 JM 号")
        normalized_album_id = self._normalize_id(
            catalog.album_id,
            "漫画 JM 号",
        )
        with self._lock:
            album = self._albums.get(normalized_album_id)
        prepared_chapters = tuple(
            (value.photo_id, value.index)
            for value in catalog.chapters
        )
        cached_chapters = (
            tuple(
                (value.photo_id, value.index)
                for value in album.catalog.chapters
            )
            if album is not None
            else ()
        )
        if album is None or cached_chapters != prepared_chapters:
            await asyncio.to_thread(
                self._fetch_catalog_sync,
                normalized_album_id,
            )
            with self._lock:
                album = self._albums.get(normalized_album_id)
        chapter = (
            album.chapters.get(normalized_photo_id)
            if album is not None
            else None
        )
        if chapter is None:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "所选本地章节已不可用，请刷新本地漫画库",
            )
        total = len(chapter.pages)
        pages = tuple(
            ReaderPageSnapshot(
                normalized_photo_id,
                page_number,
                total,
                ReaderPageState.PLACEHOLDER,
            )
            for page_number in range(1, total + 1)
        )
        return chapter.snapshot, pages

    async def fetch_page(
        self,
        photo_id: str,
        page_number: int,
        *,
        current_page: int,
        pinned_keys=(),
    ) -> tuple[str, ReaderPageSnapshot]:
        del pinned_keys
        self._ensure_open()
        normalized_photo_id = self._normalize_id(photo_id, "章节 JM 号")
        self._validate_page_number(page_number)
        self._validate_page_number(current_page)
        return await asyncio.to_thread(
            self._fetch_page_sync,
            normalized_photo_id,
            page_number,
        )

    def update_cache_window(
        self,
        photo_id: str,
        *,
        current_page: int,
        visible_pages,
    ) -> tuple[str, ...]:
        self._ensure_open()
        self._normalize_id(photo_id, "章节 JM 号")
        self._validate_page_number(current_page)
        tuple(visible_pages)
        return ()

    def _fetch_catalog_sync(self, album_id: str) -> ChapterCatalogSnapshot:
        normalized_id = self._normalize_id(album_id, "漫画 JM 号")
        try:
            manifest = self._manifest_store.load(normalized_id)
        except ChapterManifestError:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地章节清单不可用，请先修复或重新识别章节",
            ) from None
        if manifest is None:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "没有可用的本地章节清单",
            )

        root = self._require_directory(self.paths.pictures, None)
        album_dir = self._require_directory(root / normalized_id, root)
        title_dir = self._require_directory(
            album_dir / manifest.album_dir_name,
            album_dir,
        )
        chapters: dict[str, _LocalChapter] = {}
        for value in sorted(manifest.chapters, key=lambda item: item.index):
            directory = title_dir
            if value.dir_name:
                try:
                    directory = self._require_directory(
                        title_dir / value.dir_name,
                        title_dir,
                    )
                except ReaderServiceError:
                    continue
            try:
                images = LibraryService._list_direct_images(directory)
            except Exception:
                continue
            if (
                not 1 <= value.page_count <= MAX_READER_CHAPTER_PAGES
                or len(images) != value.page_count
                or not LibraryService._images_match_format(
                    images,
                    value.image_format,
                )
            ):
                continue
            pages = []
            for image_path in images:
                try:
                    signature = self._validate_image_path(
                        image_path,
                        directory,
                    )
                except ReaderServiceError:
                    pages = []
                    break
                if not LibraryService._is_valid_image_file(image_path):
                    pages = []
                    break
                pages.append(_LocalPage(image_path, signature))
            if len(pages) != value.page_count:
                continue
            snapshot = ReaderChapterSnapshot(
                photo_id=value.photo_id,
                index=value.index,
                title=value.title,
                page_count=value.page_count,
            )
            chapters[value.photo_id] = _LocalChapter(snapshot, tuple(pages))

        if not chapters:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "没有图片完整的本地章节，请先在章节管理中修复",
            )
        catalog = ChapterCatalogSnapshot(
            album_id=normalized_id,
            title=manifest.album_title,
            chapters=tuple(
                ChapterSnapshot(
                    photo_id=value.snapshot.photo_id,
                    index=value.snapshot.index,
                    title=value.snapshot.title,
                    downloaded=True,
                )
                for value in chapters.values()
            ),
        )
        with self._lock:
            self._ensure_open()
            self._albums[normalized_id] = _LocalAlbum(catalog, chapters)
        return catalog

    def _fetch_page_sync(
        self,
        photo_id: str,
        page_number: int,
    ) -> tuple[str, ReaderPageSnapshot]:
        with self._lock:
            chapter = next(
                (
                    album.chapters[photo_id]
                    for album in self._albums.values()
                    if photo_id in album.chapters
                ),
                None,
            )
        if chapter is None or page_number > len(chapter.pages):
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地章节尚未加载或页码已经失效",
            )
        page = chapter.pages[page_number - 1]
        directory = page.path.parent
        signature = self._validate_image_path(page.path, directory)
        if signature != page.signature:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片已被替换，请刷新本地漫画库后重试",
            )
        try:
            with Image.open(page.path) as image:
                width, height = image.size
                image.verify()
            after = self._file_signature(page.path)
        except (OSError, ValueError, UnidentifiedImageError):
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片不可用，请检查文件后重试",
            ) from None
        if after != page.signature:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片读取期间发生变化，请刷新后重试",
            )
        if (
            width < 1
            or height < 1
            or width > MAX_READER_IMAGE_SIDE
            or height > MAX_READER_IMAGE_SIDE
            or width * height > MAX_READER_IMAGE_PIXELS
        ):
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DIMENSIONS_EXCEEDED,
                "本地图片尺寸超过在线阅读安全上限",
            )
        snapshot = ReaderPageSnapshot(
            photo_id=photo_id,
            page_number=page_number,
            total_pages=len(chapter.pages),
            state=ReaderPageState.READY,
            width=width,
            height=height,
            cache_path=page.path,
        )
        return f"local:{photo_id}:{page_number}", snapshot

    @classmethod
    def _validate_image_path(
        cls,
        path: Path,
        directory: Path,
    ) -> tuple[int, int]:
        if is_linked_directory(path) or not path.is_file():
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片不可用，请检查文件后重试",
            )
        try:
            resolved_directory = directory.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except OSError:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片路径已经失效",
            ) from None
        if resolved.parent != resolved_directory:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片不在受管章节目录中",
            )
        return cls._file_signature(resolved)

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        try:
            state = path.stat()
        except OSError:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_DAMAGED,
                "本地图片不可用，请检查文件后重试",
            ) from None
        if state.st_size < 1:
            raise ReaderServiceError(
                ReaderErrorKind.IMAGE_EMPTY,
                "本地图片为空",
            )
        return state.st_size, state.st_mtime_ns

    @staticmethod
    def _require_directory(path: Path, parent: Path | None) -> Path:
        if is_linked_directory(path) or not path.is_dir():
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地章节目录不可用",
            )
        try:
            resolved = path.resolve(strict=True)
            resolved_parent = (
                parent.resolve(strict=True) if parent is not None else None
            )
        except OSError:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地章节目录无法安全解析",
            ) from None
        if resolved_parent is not None and resolved.parent != resolved_parent:
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地章节目录不在受管范围内",
            )
        return resolved

    def _ensure_open(self) -> None:
        if self._closed:
            raise ReaderServiceError(
                ReaderErrorKind.INTERNAL,
                "本地阅读服务已经关闭",
            )

    @staticmethod
    def _normalize_id(value: str, label: str) -> str:
        try:
            return str(int(normalize_album_id(value)))
        except (InvalidAlbumId, TypeError, ValueError):
            raise ReaderServiceError(
                ReaderErrorKind.NOT_FOUND,
                f"{label}无效",
            ) from None

    @staticmethod
    def _validate_page_number(value: int) -> None:
        if (
            type(value) is not int
            or not 1 <= value <= MAX_READER_CHAPTER_PAGES
        ):
            raise ReaderServiceError(
                ReaderErrorKind.CHAPTER_UNAVAILABLE,
                "本地图片页码无效",
            )


__all__ = ["LocalReaderService"]
