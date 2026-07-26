from dataclasses import dataclass
from enum import Enum
from pathlib import Path


MAX_CHAPTERS_PER_TASK = 10
DOWNLOAD_ENGINES = frozenset({"async", "sync"})
API_ROUTES = frozenset(
    {
        "auto",
        "www.cdnplaystation6.cc",
        "www.cdnaspa.club",
        "www.cdnplaystation6.vip",
        "www.cdnaspa.vip",
    }
)
PACKAGE_FORMATS = frozenset({"pdf", "cbz", "images"})
IMAGE_FORMATS = frozenset({"jpg", "png"})


class TaskStatus(str, Enum):
    PENDING = "pending"
    FETCHING = "fetching"
    DOWNLOADING = "downloading"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"


class SearchMode(str, Enum):
    GENERAL = "general"
    AUTHOR = "author"
    TAG = "tag"
    EXACT_ID = "exact_id"


class AccountStatus(str, Enum):
    SIGNED_OUT = "signed_out"
    RESTORING = "restoring"
    SAVED_SESSION = "saved_session"
    SIGNING_IN = "signing_in"
    SIGNED_IN = "signed_in"
    EXPIRED = "expired"
    LOCAL_DATA_UNREADABLE = "local_data_unreadable"


class LibraryLayout(str, Enum):
    MANAGED = "managed"
    LEGACY = "legacy"
    UNVERIFIED = "unverified"


class ChapterImageStatus(str, Enum):
    COMPLETE = "complete"
    MISSING = "missing"
    DAMAGED = "damaged"


class ChapterPackageStatus(str, Enum):
    COMPLETE = "complete"
    MISSING = "missing"
    DAMAGED = "damaged"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TaskConfig:
    download_engine: str = "async"
    api_route: str = "auto"
    package_format: str = "pdf"
    image_format: str = "jpg"
    image_concurrency: int = 16
    multi_chapter_download_behavior: str = "parallel"

    def validate(self) -> None:
        if self.download_engine not in DOWNLOAD_ENGINES:
            raise ValueError("下载引擎无效")
        if self.api_route not in API_ROUTES:
            raise ValueError("API 路线无效")
        if self.package_format not in PACKAGE_FORMATS:
            raise ValueError("打包格式无效")
        if self.image_format not in IMAGE_FORMATS:
            raise ValueError("图片格式无效")
        if (
            type(self.image_concurrency) is not int
            or not 1 <= self.image_concurrency <= 64
        ):
            raise ValueError("图片并发数必须在 1 到 64 之间")
        if self.multi_chapter_download_behavior not in {"parallel", "queued"}:
            raise ValueError("多章漫画下载行为无效")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    mode: SearchMode
    query: str
    page: int = 1


@dataclass(frozen=True, slots=True)
class ChapterSnapshot:
    photo_id: str
    index: int
    title: str
    downloaded: bool = False


@dataclass(frozen=True, slots=True)
class ChapterCatalogSnapshot:
    album_id: str
    title: str | None
    chapters: tuple[ChapterSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ChapterManifestEntry:
    photo_id: str
    index: int
    title: str
    dir_name: str
    page_count: int
    image_format: str | None = None
    downloaded_at_utc: str | None = None
    package_format: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryChapterSnapshot:
    """Immutable offline status of one managed chapter for the library UI."""

    album_id: str
    photo_id: str
    index: int
    title: str
    image_directory: Path | None
    package_path: Path | None
    page_count: int
    valid_image_count: int
    image_status: ChapterImageStatus
    package_format: str | None
    package_status: ChapterPackageStatus
    downloaded_at_utc: str | None
    can_rebuild: bool
    can_redownload: bool
    can_delete_images: bool
    can_delete_package: bool
    can_delete_all: bool
    suggested_package_format: str | None = None
    problem_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChapterManifest:
    version: int
    album_id: str
    album_title: str
    album_dir_name: str
    chapters: tuple[ChapterManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class SearchResultSnapshot:
    album_id: str
    title: str | None
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    chapter_catalog: ChapterCatalogSnapshot | None = None


@dataclass(frozen=True, slots=True)
class SearchPageSnapshot:
    request: SearchRequest
    total: int
    page_count: int
    items: tuple[SearchResultSnapshot, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    status: AccountStatus
    username: str | None = None
    last_verified_at_utc: str | None = None


@dataclass(frozen=True, slots=True)
class FavoriteItemSnapshot:
    album_id: str
    title: str | None
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FavoriteFolderSnapshot:
    folder_id: str
    name: str
    items: tuple[FavoriteItemSnapshot, ...]


@dataclass(frozen=True, slots=True)
class FavoritesSnapshot:
    synced_at_utc: str | None
    folders: tuple[FavoriteFolderSnapshot, ...]
    order_by: str = "mr"

    def __post_init__(self):
        if self.order_by not in {"mr", "mp"}:
            raise ValueError("order_by must be mr or mp")


@dataclass(frozen=True, slots=True)
class FavoritesFilterSnapshot:
    folder_id: str
    keyword: str
    items: tuple[FavoriteItemSnapshot, ...]


@dataclass(frozen=True, slots=True)
class FavoritesSyncProgress:
    folder_index: int
    folder_count: int
    folder_name: str
    page: int
    page_count: int
    received_items: int
    expected_items: int


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    album_id: str
    title: str | None
    status: TaskStatus
    progress: int
    chapter: str
    page: str
    preview_path: Path | None
    preview_revision: int
    pdf_directory: Path | None
    error: str | None
    cover_url: str | None
    selected_chapter_ids: tuple[str, ...] | None = None
    config: TaskConfig = TaskConfig()
    cbz_directory: Path | None = None
    force_redownload_chapter_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryItem:
    album_id: str
    title: str | None
    layout: LibraryLayout
    chapter_count: int
    image_count: int
    image_size: int
    preview_path: Path | None
    pdf_directory: Path | None
    pdf_size: int
    cbz_directory: Path | None = None
    cbz_size: int = 0
    downloaded_at_utc: str | None = None

    @property
    def has_images(self) -> bool:
        return self.image_count > 0

    @property
    def has_pdf(self) -> bool:
        return self.pdf_directory is not None

    @property
    def has_cbz(self) -> bool:
        return self.cbz_directory is not None
