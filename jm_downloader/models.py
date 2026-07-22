from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class ChapterCatalogSnapshot:
    album_id: str
    title: str | None
    chapters: tuple[ChapterSnapshot, ...]


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
    pdf_path: Path | None
    error: str | None
    cover_url: str | None
    selected_chapter_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class LibraryItem:
    album_id: str
    chapter_count: int
    image_count: int
    image_size: int
    preview_path: Path | None
    pdf_path: Path | None
    pdf_size: int

    @property
    def has_images(self) -> bool:
        return self.preview_path is not None

    @property
    def has_pdf(self) -> bool:
        return self.pdf_path is not None
