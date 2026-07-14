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


@dataclass(frozen=True, slots=True)
class SearchRequest:
    mode: SearchMode
    query: str
    page: int = 1


@dataclass(frozen=True, slots=True)
class SearchResultSnapshot:
    album_id: str
    title: str | None
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchPageSnapshot:
    request: SearchRequest
    total: int
    page_count: int
    items: tuple[SearchResultSnapshot, ...]
    truncated: bool = False


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
