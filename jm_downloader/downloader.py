import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jmcomic
from PIL import Image, UnidentifiedImageError
from common import suffix_not_equal

from .jmcomic_client import serialized_client_construction
from .jmcomic_logging import install_safe_jmcomic_logging
from .library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestError,
    ChapterManifestStore,
    CorruptChapterManifest,
    UnsupportedChapterManifestVersion,
)
from .models import (
    MAX_CHAPTERS_PER_TASK,
    ChapterManifest,
    ChapterManifestEntry,
    TaskConfig,
)
from .option_config import apply_api_route
from .packaging import chapter_to_cbz
from .pdf import (
    IMAGE_EXTENSIONS,
    PART_FILE_MARKER,
    PdfPublishAborted,
    PdfSourcePathError,
    chapter_to_pdf,
    find_album_images,
    is_linked_directory,
    natural_key,
)
from .settings import AppPaths, DEFAULT_PATHS


LOGGER = logging.getLogger("jm-downloader")
MANAGED_PATH_LIMIT = 240
_INVALID_WINDOWS_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def sanitize_album_directory_name(
    title: str | None,
    album_id: str,
    paths: AppPaths,
) -> str:
    album_id = str(album_id)
    value = title if isinstance(title, str) else ""
    value = _INVALID_WINDOWS_COMPONENT.sub("_", value)
    value = value.strip(" .")
    if not value:
        value = album_id
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"

    pictures_root = str(paths.pictures.resolve())
    pdf_root = str(paths.pdfs.resolve())
    image_overhead = (
        len(pictures_root)
        + 1
        + len(album_id)
        + len("\\第999999章\\00000.jpg")
    )
    single_pdf_overhead = (
        len(pdf_root)
        + 1
        + len(album_id)
        + len("\\\\.pdf")
    )
    image_budget = MANAGED_PATH_LIMIT - image_overhead
    pdf_budget = (MANAGED_PATH_LIMIT - single_pdf_overhead) // 2
    budget = min(100, image_budget, pdf_budget)
    if budget < 1:
        raise ManagedPathError("configured output path is too long")
    value = value[:budget].strip(" .")
    if not value:
        value = album_id[:budget].strip(" .")
    if not value:
        raise ManagedPathError("album directory name has no safe path budget")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
        value = value[:budget].strip(" .")
    return value


class DownloadStopped(Exception):
    pass


class DownloadIntegrityError(Exception):
    pass


class ImageValidationError(DownloadIntegrityError):
    pass


class ManagedPathError(DownloadIntegrityError):
    pass


class SelectedChapterUnavailable(DownloadIntegrityError):
    pass


class LegacyChapterSelectionRequired(DownloadIntegrityError):
    pass


class PdfPackagingError(Exception):
    pass


class DownloadWorker:
    PDF_PCT = 95
    REQUEST_TIMEOUT_SECONDS = 60
    REQUEST_RETRIES = 3

    def __init__(
        self,
        album_id: str,
        on_progress=None,
        on_complete=None,
        on_error=None,
        on_info=None,
        on_preview=None,
        on_stopped=None,
        paths: AppPaths = DEFAULT_PATHS,
        image_concurrency: int = 16,
        selected_chapter_ids: tuple[str, ...] | None = None,
        multi_chapter_download_behavior: str = "parallel",
        task_config: TaskConfig | None = None,
        force_redownload_chapter_ids: tuple[str, ...] = (),
    ):
        self.album_id = str(album_id)
        self.on_progress = on_progress or (lambda *args: None)
        self.on_complete = on_complete or (lambda *args: None)
        self.on_error = on_error or (lambda *args: None)
        self.on_info = on_info or (lambda *args: None)
        self.on_preview = on_preview or (lambda *args: None)
        self.on_stopped = on_stopped or (lambda *args: None)
        self.paths = paths
        if task_config is None:
            if multi_chapter_download_behavior not in {
                "parallel",
                "queued",
            }:
                raise ValueError(
                    "multi_chapter_download_behavior must be parallel or queued"
                )
            task_config = TaskConfig(
                download_engine="sync",
                image_concurrency=max(1, int(image_concurrency)),
                multi_chapter_download_behavior=(
                    multi_chapter_download_behavior
                ),
            )
        task_config.validate()
        self.task_config = task_config
        self.download_engine = task_config.download_engine
        self.image_concurrency = task_config.image_concurrency
        self.selected_chapter_ids = self._normalize_selected_chapter_ids(
            selected_chapter_ids
        )
        self.force_redownload_chapter_ids = (
            self._normalize_force_redownload_chapter_ids(
                force_redownload_chapter_ids
            )
        )
        self.multi_chapter_download_behavior = (
            task_config.multi_chapter_download_behavior
        )
        self._stop_flag = threading.Event()
        self._thread = None
        self._total_photos = 0
        self._album_total_known = False
        self._downloaded_count = 0
        self._progress_lock = threading.Lock()
        self._integrity_lock = threading.Lock()
        self._expected_images: set[Path] = set()
        self._verified_images: set[Path] = set()
        self._active_downloader = None
        self._preview_path = None
        self._manifest_store = ChapterManifestStore(paths)
        self._pending_manifest: ChapterManifest | None = None
        self._album_dir_name: str | None = None
        self._album_title = f"JM {self.album_id}"
        self._replacement_backups: list[tuple[Path, Path]] = []
        self.paths.ensure_output_directories()

    def _make_option(self):
        install_safe_jmcomic_logging()
        option = jmcomic.create_option_by_file(str(self.paths.option_file))
        option.download.threading.image = self.image_concurrency
        option.download.threading.photo = (
            2 if self.multi_chapter_download_behavior == "parallel" else 1
        )
        option.download.image.suffix = f".{self.task_config.image_format}"
        apply_api_route(option, self.task_config.api_route)
        option.client.retry_times = self.REQUEST_RETRIES
        option.client.postman.meta_data.timeout = self.REQUEST_TIMEOUT_SECONDS
        option.dir_rule = jmcomic.DirRule(
            "Bd/Aid/Ajm_downloader_album_dir/Pjm_downloader_chapter_dir",
            base_dir=str(self.paths.pictures),
        )
        return option

    def fetch_info(self):
        install_safe_jmcomic_logging()
        try:
            option = self._make_option()
            with serialized_client_construction():
                client = option.build_jm_client()
            album = client.get_album_detail(self.album_id)
            title = album.title if hasattr(album, "title") else album.name
            cover_url = album.cover if hasattr(album, "cover") else None
            total = getattr(album, "page_count", 0) or 0
            return title, cover_url, total
        except Exception:
            return None, None, 0

    def run(self):
        try:
            install_safe_jmcomic_logging()
            if self._stop_flag.is_set():
                return
            option = self._make_option()
            album_dir = self.paths.pictures / self.album_id
            album_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_stale_parts(album_dir)
            option.dir_rule.base_dir = str(self.paths.pictures)
            owner = self

            class ProgressDownloader(jmcomic.JmDownloader):
                def create_client(self):
                    with serialized_client_construction():
                        return super().create_client()

                def __init__(self, active_option):
                    super().__init__(active_option)
                    owner._active_downloader = self

                def do_filter(self, detail):
                    values = super().do_filter(detail)
                    is_album = getattr(detail, "is_album", None)
                    if not callable(is_album) or not is_album():
                        return values
                    return owner._prepare_selected_photos(
                        self,
                        detail,
                        tuple(values),
                    )

                def before_album(self, album):
                    owner._prepare_album(album)
                    super().before_album(album)
                    owner._total_photos = getattr(album, "page_count", 0) or 0
                    owner._album_total_known = owner._total_photos > 0
                    title = owner._album_title
                    cover = getattr(album, "cover", None)
                    owner.on_info(owner.album_id, title, cover)

                def before_photo(self, photo):
                    super().before_photo(photo)
                    if not owner._album_total_known:
                        with owner._progress_lock:
                            owner._total_photos += len(photo)

                def download_by_image_detail(self, image):
                    try:
                        owner._download_image(self, image)
                    except DownloadStopped:
                        return
                    except Exception as error:
                        self.download_failed_image.append((image, error))

                def execute_on_condition(
                    self,
                    iter_objs,
                    apply,
                    count_batch,
                ):
                    items = list(self.do_filter(iter_objs))
                    if not items or owner._stop_flag.is_set():
                        return
                    if type(count_batch) is not int or count_batch < 1:
                        worker_count = len(items)
                    else:
                        worker_count = min(count_batch, len(items))

                    def apply_unless_stopped(item):
                        if owner._stop_flag.is_set():
                            return
                        try:
                            apply(item)
                        except DownloadStopped:
                            return
                        except Exception:
                            return

                    with ThreadPoolExecutor(
                        max_workers=max(1, worker_count),
                        thread_name_prefix="jm-download",
                    ) as executor:
                        futures = [
                            executor.submit(apply_unless_stopped, item)
                            for item in items
                        ]
                        for future in futures:
                            future.result()

            class AsyncProgressDownloader(jmcomic.JmAsyncDownloader):
                def __init__(self, active_option):
                    super().__init__(active_option)
                    owner._active_downloader = self

                async def __aenter__(self):
                    with serialized_client_construction():
                        self.client = self.option.new_jm_async_client(
                            max_clients=self._image_concurrency
                        )
                    try:
                        await self.client.setup()
                    except BaseException:
                        try:
                            await self.client.close()
                        except BaseException as cleanup_error:
                            LOGGER.warning(
                                "Async downloader setup cleanup failed (%s)",
                                type(cleanup_error).__name__,
                            )
                        finally:
                            self.client = None
                            self.shutdown()
                        raise
                    return self

                async def before_album(self, album):
                    owner._prepare_album(album)
                    await super().before_album(album)
                    title = owner._album_title
                    cover = getattr(album, "cover", None)
                    owner.on_info(owner.album_id, title, cover)

                async def download_by_album_detail(self, album):
                    await self.before_album(album)
                    if album.skip:
                        return
                    photos = await owner._prepare_selected_photos_async(
                        self,
                        album,
                        tuple(album),
                    )
                    if photos:
                        await asyncio.gather(
                            *(self._safe_download_photo(photo) for photo in photos)
                        )
                    await self.after_album(album)

                async def _download_single_image(self, image):
                    await owner._download_image_async(self, image)

            if self.download_engine == "async":
                asyncio.run(
                    jmcomic.download_album_async(
                        self.album_id,
                        option,
                        downloader=AsyncProgressDownloader,
                        check_exception=False,
                    )
                )
            else:
                jmcomic.download_album(
                    self.album_id,
                    option,
                    downloader=ProgressDownloader,
                    check_exception=False,
                )
            if self._stop_flag.is_set():
                return

            self._verify_download_result()
            self._mark_manifest_downloaded()
            self._commit_replacements()

            artifact_directory = None
            if self.task_config.package_format == "pdf":
                self.on_progress(
                    self.album_id, self.PDF_PCT, "打包 PDF", ""
                )
                artifact_directory = self._package_chapter_pdfs()
            elif self.task_config.package_format == "cbz":
                self.on_progress(
                    self.album_id, self.PDF_PCT, "打包 CBZ", ""
                )
                artifact_directory = self._package_chapter_cbz()
            if self._stop_flag.is_set():
                return
            if self._pending_manifest is None:
                raise ChapterManifestError("没有可发布的章节清单")
            self._manifest_store.merge_and_save(self._pending_manifest)
            if self._stop_flag.is_set():
                return
            self.on_complete(
                self.album_id,
                (
                    str(artifact_directory)
                    if artifact_directory is not None
                    else None
                ),
            )
        except (DownloadStopped, PdfPublishAborted):
            return
        except Exception as error:
            LOGGER.error(
                "Download failed for JM %s (%s)",
                self.album_id,
                type(error).__name__,
            )
            self.on_error(
                self.album_id,
                self._public_error_message(error),
            )
        finally:
            self._rollback_replacements()
            try:
                self.on_stopped(self.album_id)
            except Exception:
                LOGGER.exception(
                    "Download stopped callback failed for JM %s",
                    self.album_id,
                )

    def _prepare_album(self, album) -> None:
        try:
            photo_count = len(album)
        except (TypeError, ValueError) as error:
            raise DownloadIntegrityError("album chapter count is invalid") from error
        if type(photo_count) is not int or photo_count < 1:
            raise DownloadIntegrityError("album has no chapters")
        if self.selected_chapter_ids is None and photo_count > 1:
            raise LegacyChapterSelectionRequired()

        title = getattr(album, "title", None) or getattr(album, "name", None)
        if not isinstance(title, str) or not title.strip():
            title = f"JM {self.album_id}"
        self._album_title = title

        existing = None
        try:
            existing = self._manifest_store.load(self.album_id)
        except UnsupportedChapterManifestVersion:
            raise
        except CorruptChapterManifest:
            existing = None
        self._album_dir_name = (
            existing.album_dir_name
            if existing is not None
            else sanitize_album_directory_name(
                self._album_title,
                self.album_id,
                self.paths,
            )
        )
        setattr(
            album,
            "jm_downloader_album_dir",
            self._album_dir_name,
        )

    def _prepare_selected_photos(
        self,
        active_downloader,
        album,
        photos: tuple,
    ) -> tuple:
        if not photos:
            if self.selected_chapter_ids is not None:
                raise SelectedChapterUnavailable()
            raise DownloadIntegrityError("album has no chapters")
        selected = (
            None
            if self.selected_chapter_ids is None
            else set(self.selected_chapter_ids)
        )
        if selected is None:
            filtered = photos
        else:
            filtered = tuple(
                photo
                for photo in photos
                if self._photo_id(photo) in selected
            )
            found = {self._photo_id(photo) for photo in filtered}
            if found != selected or len(filtered) != len(found):
                raise SelectedChapterUnavailable()

        photo_ids = [self._photo_id(photo) for photo in filtered]
        if None in photo_ids or len(photo_ids) != len(set(photo_ids)):
            raise SelectedChapterUnavailable()

        actual_single_album = len(photos) == 1
        total = 0
        entries = []
        seen_indexes = set()
        for photo, photo_id in zip(filtered, photo_ids, strict=True):
            if self._stop_flag.is_set():
                raise DownloadStopped()
            active_downloader.client.check_photo(photo)
            index = self._photo_index(photo)
            if index in seen_indexes:
                raise SelectedChapterUnavailable()
            seen_indexes.add(index)
            try:
                page_count = len(photo)
            except (TypeError, ValueError) as error:
                raise DownloadIntegrityError(
                    "chapter page count is invalid"
                ) from error
            if type(page_count) is not int or page_count < 1:
                raise DownloadIntegrityError("chapter has no images")
            total += page_count
            title = getattr(photo, "title", None) or getattr(photo, "name", None)
            if not isinstance(title, str) or not title.strip():
                title = f"第 {index} 章"
            dir_name = "" if actual_single_album else f"第{index}章"
            setattr(photo, "jm_downloader_chapter_dir", dir_name)
            entries.append(
                ChapterManifestEntry(
                    photo_id=photo_id,
                    index=index,
                    title=title,
                    dir_name=dir_name,
                    page_count=page_count,
                    image_format=self.task_config.image_format,
                )
            )

        if self._album_dir_name is None:
            raise DownloadIntegrityError("album directory is unavailable")
        self._pending_manifest = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id=self.album_id,
            album_title=self._album_title,
            album_dir_name=self._album_dir_name,
            chapters=tuple(sorted(entries, key=lambda entry: entry.index)),
        )
        self._total_photos = total
        self._album_total_known = total > 0
        self._stage_forced_chapters()
        return filtered

    async def _prepare_selected_photos_async(
        self,
        active_downloader,
        album,
        photos: tuple,
    ) -> tuple:
        if not photos:
            if self.selected_chapter_ids is not None:
                raise SelectedChapterUnavailable()
            raise DownloadIntegrityError("album has no chapters")
        selected = (
            None
            if self.selected_chapter_ids is None
            else set(self.selected_chapter_ids)
        )
        if selected is None:
            filtered = photos
        else:
            filtered = tuple(
                photo
                for photo in photos
                if self._photo_id(photo) in selected
            )
            found = {self._photo_id(photo) for photo in filtered}
            if found != selected or len(filtered) != len(found):
                raise SelectedChapterUnavailable()

        photo_ids = [self._photo_id(photo) for photo in filtered]
        if None in photo_ids or len(photo_ids) != len(set(photo_ids)):
            raise SelectedChapterUnavailable()

        actual_single_album = len(photos) == 1
        total = 0
        entries = []
        seen_indexes = set()
        for photo, photo_id in zip(filtered, photo_ids, strict=True):
            if self._stop_flag.is_set():
                raise DownloadStopped()
            await active_downloader.client.check_photo(photo)
            index = self._photo_index(photo)
            if index in seen_indexes:
                raise SelectedChapterUnavailable()
            seen_indexes.add(index)
            try:
                page_count = len(photo)
            except (TypeError, ValueError) as error:
                raise DownloadIntegrityError(
                    "chapter page count is invalid"
                ) from error
            if type(page_count) is not int or page_count < 1:
                raise DownloadIntegrityError("chapter has no images")
            total += page_count
            title = getattr(photo, "title", None) or getattr(
                photo, "name", None
            )
            if not isinstance(title, str) or not title.strip():
                title = f"第 {index} 章"
            dir_name = "" if actual_single_album else f"第{index}章"
            setattr(photo, "jm_downloader_chapter_dir", dir_name)
            entries.append(
                ChapterManifestEntry(
                    photo_id=photo_id,
                    index=index,
                    title=title,
                    dir_name=dir_name,
                    page_count=page_count,
                    image_format=self.task_config.image_format,
                )
            )

        if self._album_dir_name is None:
            raise DownloadIntegrityError("album directory is unavailable")
        self._pending_manifest = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id=self.album_id,
            album_title=self._album_title,
            album_dir_name=self._album_dir_name,
            chapters=tuple(sorted(entries, key=lambda entry: entry.index)),
        )
        self._total_photos = total
        self._album_total_known = total > 0
        self._stage_forced_chapters()
        return filtered

    @staticmethod
    def _photo_index(photo) -> int:
        value = getattr(photo, "album_index", None)
        if type(value) is not int:
            if isinstance(value, str) and value.isascii() and value.isdigit():
                value = int(value)
            else:
                raise SelectedChapterUnavailable()
        if value < 1:
            raise SelectedChapterUnavailable()
        return value

    def _download_image(self, downloader, image) -> None:
        final_path = self._managed_image_path(
            Path(downloader.option.decide_image_filepath(image))
        )
        with self._integrity_lock:
            self._expected_images.add(final_path)

        image.save_path = str(final_path)
        image.exists = final_path.is_file()
        image.cache = downloader.option.decide_download_cache(image)

        if image.exists and not self._is_valid_image(final_path):
            final_path.unlink()
            image.exists = False

        downloader.before_image(image, str(final_path))
        if image.skip:
            return

        if image.cache and image.exists:
            self._record_verified_image(downloader, image, final_path)
            return
        if self._stop_flag.is_set():
            raise DownloadStopped()

        final_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=final_path.parent,
            prefix=f".{final_path.stem}{PART_FILE_MARKER}",
            suffix=final_path.suffix,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            decode_image = downloader.option.decide_download_image_decode(image)
            downloader.client.download_by_image_detail(
                image,
                str(temp_path),
                decode_image=decode_image,
            )
            if not self._is_valid_image(temp_path):
                raise ImageValidationError("downloaded image is invalid")
            os.replace(temp_path, final_path)
            self._record_verified_image(downloader, image, final_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _record_verified_image(self, downloader, image, path: Path) -> None:
        with self._integrity_lock:
            self._verified_images.add(path)
        downloader.after_image(image, str(path))
        self._on_image_ready(image, str(path))

    async def _download_image_async(self, downloader, image) -> None:
        if self._stop_flag.is_set():
            raise DownloadStopped()
        final_path = self._managed_image_path(
            Path(downloader.option.decide_image_filepath(image))
        )
        with self._integrity_lock:
            self._expected_images.add(final_path)

        image.save_path = str(final_path)
        image.exists = final_path.is_file()
        image.cache = downloader.option.decide_download_cache(image)
        if image.exists and not self._is_valid_image(final_path):
            final_path.unlink()
            image.exists = False

        await downloader.before_image(image, str(final_path))
        if image.skip:
            return
        if image.cache and image.exists:
            await self._record_verified_image_async(
                downloader, image, final_path
            )
            return
        if self._stop_flag.is_set():
            raise DownloadStopped()

        final_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=final_path.parent,
            prefix=f".{final_path.stem}{PART_FILE_MARKER}",
            suffix=final_path.suffix,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            async with downloader._image_semaphore:
                if self._stop_flag.is_set():
                    raise DownloadStopped()
                response = await downloader.client.get_jm_image(
                    image.download_url
                )
                image_bytes = response.content
                decode_image = (
                    downloader.option.decide_download_image_decode(image)
                )
                loop = asyncio.get_running_loop()
                if decode_image and image.scramble_id:
                    await loop.run_in_executor(
                        downloader._decode_pool,
                        downloader._decode_and_save,
                        image_bytes,
                        int(image.scramble_id),
                        int(image.aid),
                        image.img_file_name,
                        str(temp_path),
                    )
                else:
                    image_url = image.download_url.split("?", 1)[0]
                    await loop.run_in_executor(
                        downloader._decode_pool,
                        downloader._save_raw,
                        image_bytes,
                        str(temp_path),
                        suffix_not_equal(image_url, str(temp_path)),
                    )
            if not self._is_valid_image(temp_path):
                raise ImageValidationError("downloaded image is invalid")
            if self._stop_flag.is_set():
                raise DownloadStopped()
            os.replace(temp_path, final_path)
            await self._record_verified_image_async(
                downloader, image, final_path
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def _record_verified_image_async(
        self,
        downloader,
        image,
        path: Path,
    ) -> None:
        with self._integrity_lock:
            self._verified_images.add(path)
        await downloader.after_image(image, str(path))
        self._on_image_ready(image, str(path))

    def _verify_download_result(self) -> None:
        downloader = self._active_downloader
        if downloader is None:
            raise DownloadIntegrityError("downloader result is unavailable")
        failures = [
            error
            for _detail, error in (
                *downloader.download_failed_image,
                *downloader.download_failed_photo,
            )
        ]
        if failures:
            for error in failures:
                if isinstance(
                    error,
                    (
                        ManagedPathError,
                        ImageValidationError,
                        PermissionError,
                        OSError,
                        ConnectionError,
                        TimeoutError,
                    ),
                ):
                    raise error
            raise DownloadIntegrityError("upstream reported partial download")

        with self._integrity_lock:
            expected = set(self._expected_images)
            verified = set(self._verified_images)
        if not expected:
            raise DownloadIntegrityError("no images were discovered")
        if self._album_total_known and len(expected) != self._total_photos:
            raise DownloadIntegrityError("expected image count does not match album")
        if expected != verified:
            raise DownloadIntegrityError("not every expected image was verified")
        if any(not self._is_valid_image(path) for path in expected):
            raise DownloadIntegrityError("published image validation failed")

    def _package_chapter_pdfs(self) -> Path:
        manifest = self._pending_manifest
        if manifest is None or not manifest.chapters:
            raise ChapterManifestError("没有可用于打包的章节清单")

        pdf_directory = self._prepare_pdf_directory(
            manifest.album_dir_name
        )
        image_directory = (
            self.paths.pictures
            / self.album_id
            / manifest.album_dir_name
        )
        for chapter in manifest.chapters:
            if self._stop_flag.is_set():
                raise DownloadStopped()
            chapter_directory = (
                image_directory / chapter.dir_name
                if chapter.dir_name
                else image_directory
            )
            try:
                direct_images = tuple(
                    candidate
                    for candidate in chapter_directory.iterdir()
                    if candidate.suffix.lower() in IMAGE_EXTENSIONS
                    and self._is_valid_image(candidate)
                )
            except OSError as error:
                raise PdfPackagingError(
                    f"chapter {chapter.index} cannot be read"
                ) from error
            if len(direct_images) != chapter.page_count:
                raise DownloadIntegrityError(
                    f"chapter {chapter.index} image count does not match"
                )
            output_name = (
                f"{chapter.dir_name}.pdf"
                if chapter.dir_name
                else f"{manifest.album_dir_name}.pdf"
            )
            output_path = pdf_directory / output_name
            if output_path.is_symlink():
                raise ManagedPathError("PDF output is a symbolic link")
            try:
                result = chapter_to_pdf(
                    chapter_directory,
                    output_path,
                    publish_guard=lambda: not self._stop_flag.is_set(),
                )
            except PdfPublishAborted:
                raise
            except PdfSourcePathError as error:
                raise ManagedPathError(
                    f"chapter {chapter.index} path is unsafe"
                ) from error
            except Exception as error:
                raise PdfPackagingError(
                    f"PDF generation failed for chapter {chapter.index}"
                ) from error
            if result is None or Path(result).resolve() != output_path.resolve():
                raise PdfPackagingError(
                    f"PDF generator returned no output for chapter "
                    f"{chapter.index}"
                )
        return pdf_directory

    def _package_chapter_cbz(self) -> Path:
        manifest = self._pending_manifest
        if manifest is None or not manifest.chapters:
            raise ChapterManifestError("没有可用于打包的章节清单")
        output_directory = self._prepare_pdf_directory(
            manifest.album_dir_name
        )
        image_directory = (
            self.paths.pictures
            / self.album_id
            / manifest.album_dir_name
        )
        for chapter in manifest.chapters:
            if self._stop_flag.is_set():
                raise DownloadStopped()
            chapter_directory = (
                image_directory / chapter.dir_name
                if chapter.dir_name
                else image_directory
            )
            output_name = (
                f"{chapter.dir_name}.cbz"
                if chapter.dir_name
                else f"{manifest.album_dir_name}.cbz"
            )
            output_path = output_directory / output_name
            try:
                chapter_to_cbz(
                    chapter_directory,
                    output_path,
                    publish_guard=lambda: not self._stop_flag.is_set(),
                )
            except PdfPublishAborted:
                raise
            except PdfSourcePathError as error:
                raise ManagedPathError(
                    f"chapter {chapter.index} path is unsafe"
                ) from error
            except Exception as error:
                raise PdfPackagingError(
                    f"CBZ generation failed for chapter {chapter.index}"
                ) from error
        return output_directory

    def _stage_forced_chapters(self) -> None:
        manifest = self._pending_manifest
        if (
            manifest is None
            or not self.force_redownload_chapter_ids
            or self._replacement_backups
        ):
            return
        forced = set(self.force_redownload_chapter_ids)
        selected = {chapter.photo_id for chapter in manifest.chapters}
        if not forced.issubset(selected):
            raise SelectedChapterUnavailable()
        album_root = (self.paths.pictures / self.album_id).resolve()
        image_root = album_root / manifest.album_dir_name
        staged = []
        try:
            for chapter in manifest.chapters:
                if chapter.photo_id not in forced:
                    continue
                original = (
                    image_root / chapter.dir_name
                    if chapter.dir_name
                    else image_root
                )
                if not original.exists():
                    continue
                if (
                    not original.is_dir()
                    or is_linked_directory(original)
                    or not original.resolve().is_relative_to(album_root)
                ):
                    raise ManagedPathError(
                        "existing chapter path is unsafe"
                    )
                backup = album_root / (
                    f".jm-replace-{chapter.photo_id}-{uuid.uuid4().hex}"
                )
                os.replace(original, backup)
                staged.append((original, backup))
        except Exception:
            self._replacement_backups.extend(staged)
            self._rollback_replacements()
            raise
        self._replacement_backups.extend(staged)

    def _commit_replacements(self) -> None:
        for _original, backup in tuple(self._replacement_backups):
            if backup.exists():
                shutil.rmtree(backup)
        self._replacement_backups.clear()

    def _rollback_replacements(self) -> None:
        backups = tuple(reversed(self._replacement_backups))
        self._replacement_backups.clear()
        for original, backup in backups:
            if not backup.exists():
                continue
            try:
                if original.exists():
                    if (
                        not original.is_dir()
                        or is_linked_directory(original)
                    ):
                        LOGGER.error(
                            "Unsafe replacement path prevented rollback: %s",
                            original,
                        )
                        continue
                    shutil.rmtree(original)
                os.replace(backup, original)
            except OSError:
                LOGGER.exception(
                    "Failed to restore chapter replacement for JM %s",
                    self.album_id,
                )

    def _mark_manifest_downloaded(self) -> None:
        manifest = self._pending_manifest
        if manifest is None:
            raise ChapterManifestError("没有可发布的章节清单")
        completed_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        self._pending_manifest = replace(
            manifest,
            chapters=tuple(
                replace(
                    chapter,
                    image_format=self.task_config.image_format,
                    package_format=self.task_config.package_format,
                    downloaded_at_utc=completed_at,
                )
                for chapter in manifest.chapters
            ),
        )

    def _prepare_pdf_directory(self, album_dir_name: str) -> Path:
        if is_linked_directory(self.paths.pdfs):
            raise ManagedPathError("PDF root directory is a link")
        pdf_root = self.paths.pdfs.resolve()
        album_root = self.paths.pdfs / self.album_id
        if is_linked_directory(album_root):
            raise ManagedPathError("PDF album directory is a link")
        album_root.mkdir(parents=True, exist_ok=True)
        if is_linked_directory(album_root) or not album_root.is_dir():
            raise ManagedPathError("PDF album directory is invalid")

        pdf_directory = album_root / album_dir_name
        if is_linked_directory(pdf_directory):
            raise ManagedPathError("PDF title directory is a link")
        pdf_directory.mkdir(parents=True, exist_ok=True)
        if is_linked_directory(pdf_directory) or not pdf_directory.is_dir():
            raise ManagedPathError("PDF title directory is invalid")

        resolved = pdf_directory.resolve()
        try:
            relative = resolved.relative_to(pdf_root)
        except ValueError as error:
            raise ManagedPathError(
                "PDF directory escapes the managed root"
            ) from error
        if (
            len(relative.parts) != 2
            or relative.parts[0] != self.album_id
            or relative.parts[1] != album_dir_name
        ):
            raise ManagedPathError("PDF directory structure is invalid")
        return resolved

    def _managed_image_path(self, candidate: Path) -> Path:
        if not candidate.is_absolute():
            candidate = self.paths.root / candidate
        album_root = (self.paths.pictures / self.album_id).resolve()
        if is_linked_directory(album_root):
            raise ManagedPathError("album directory is a link")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(album_root):
            raise ManagedPathError("image path escapes the managed album")
        if candidate.is_symlink():
            raise ManagedPathError("image path is a symbolic link")

        current = resolved.parent
        while current != album_root:
            if is_linked_directory(current):
                raise ManagedPathError("image directory is a link")
            parent = current.parent
            if parent == current:
                raise ManagedPathError("image directory is outside the album")
            current = parent
        return resolved

    @staticmethod
    def _normalize_selected_chapter_ids(
        values,
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(
            values,
            (tuple, list),
        ):
            raise ValueError("selected_chapter_ids must be a sequence")
        result = []
        seen = set()
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 32
                or not value.isascii()
                or not value.isdigit()
            ):
                raise ValueError("selected chapter id is invalid")
            value = str(int(value))
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        if not result:
            raise ValueError("selected_chapter_ids must not be empty")
        if len(result) > MAX_CHAPTERS_PER_TASK:
            raise ValueError(
                f"selected_chapter_ids must contain at most "
                f"{MAX_CHAPTERS_PER_TASK} chapters"
            )
        return tuple(result)

    @classmethod
    def _normalize_force_redownload_chapter_ids(
        cls,
        values,
    ) -> tuple[str, ...]:
        if values in (None, ()):
            return ()
        normalized = cls._normalize_selected_chapter_ids(values)
        return normalized or ()

    @staticmethod
    def _photo_id(photo) -> str | None:
        value = getattr(photo, "photo_id", None)
        if value is None:
            value = getattr(photo, "id", None)
        try:
            value = str(value).strip()
        except Exception:
            return None
        if not value.isascii() or not value.isdigit():
            return None
        return str(int(value))

    def _cleanup_stale_parts(self, album_dir: Path) -> None:
        album_root = album_dir.resolve()
        if is_linked_directory(album_root):
            raise ManagedPathError("album directory is a link")
        for root, directories, filenames in os.walk(album_root, followlinks=False):
            root_path = Path(root)
            directories[:] = [
                name
                for name in directories
                if not is_linked_directory(root_path / name)
            ]
            for filename in filenames:
                if PART_FILE_MARKER not in filename:
                    continue
                candidate = root_path / filename
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve()
                if resolved.is_relative_to(album_root):
                    candidate.unlink(missing_ok=True)

    @staticmethod
    def _is_valid_image(path: Path) -> bool:
        try:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size <= 0
            ):
                return False
            with Image.open(path) as image:
                detected_format = str(image.format or "").upper()
                image.verify()
            expected_formats = {
                ".jpg": {"JPEG"},
                ".jpeg": {"JPEG"},
                ".png": {"PNG"},
                ".webp": {"WEBP"},
                ".bmp": {"BMP"},
                ".gif": {"GIF"},
            }.get(path.suffix.lower())
            if expected_formats is None or detected_format not in expected_formats:
                return False
            with Image.open(path) as image:
                image.load()
                return image.width > 0 and image.height > 0
        except (OSError, ValueError, UnidentifiedImageError):
            return False

    def _public_error_message(self, error: Exception) -> str:
        if isinstance(error, LegacyChapterSelectionRequired):
            return "旧任务未保存章节选择，请移除任务后重新选择"
        if isinstance(error, SelectedChapterUnavailable):
            return "所选章节已发生变化，请移除任务后重新选择"
        if isinstance(error, UnsupportedChapterManifestVersion):
            return "本地章节清单来自更高版本，当前程序无法继续"
        if isinstance(error, ChapterManifestError):
            return "章节清单无法保存，请点击继续重试"
        if isinstance(error, ManagedPathError):
            return "下载路径未通过安全检查，请检查目录设置"
        if isinstance(error, (ImageValidationError, DownloadIntegrityError)):
            return "图片不完整或已损坏，请点击继续重试"
        if isinstance(error, PdfPackagingError):
            return "PDF 生成失败，图片已保留，可稍后继续"
        if isinstance(error, PermissionError):
            return "无法写入下载目录，请检查权限和磁盘空间"
        if isinstance(error, (ConnectionError, TimeoutError)):
            if self.task_config.api_route != "auto":
                return (
                    "网络暂时不可用；固定 API 路线可能已失效，"
                    "请在设置中切回“自动选择”后重试"
                )
            return "网络暂时不可用，请检查连接后继续"
        if isinstance(error, OSError):
            return "本地文件操作失败，请检查磁盘和下载目录"
        return "下载失败，请检查网络或稍后继续"

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop_flag.set()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def _on_image_ready(self, image, image_path: str) -> None:
        path = Path(image_path).resolve()
        if not path.is_file():
            return

        with self._progress_lock:
            self._downloaded_count += 1
            if self._total_photos > 0:
                percent = min(94, int(self._downloaded_count / self._total_photos * 94))
            else:
                percent = min(94, 5 + int(self._downloaded_count / 40 * 89))

            chapter = ""
            photo = getattr(image, "from_photo", None)
            if photo is not None:
                chapter = getattr(photo, "title", None) or getattr(photo, "name", "") or ""

            self.on_progress(
                self.album_id,
                percent,
                str(chapter),
                f"{self._downloaded_count}/{self._total_photos or '?'}",
            )

            album_dir = self.paths.pictures / self.album_id
            with self._integrity_lock:
                verified = tuple(self._verified_images)
            candidate = min(
                verified,
                key=lambda path: tuple(
                    natural_key(part)
                    for part in path.relative_to(album_dir).parts
                ),
                default=None,
            )
            if candidate is not None and candidate != self._preview_path:
                self._preview_path = candidate
                self.on_preview(self.album_id, str(candidate))

    def _find_first_downloaded_image(self) -> Path | None:
        return self.find_valid_preview(self.paths.pictures / self.album_id)

    @classmethod
    def find_valid_preview(cls, album_dir: Path) -> Path | None:
        images = (
            path for path in find_album_images(album_dir) if cls._is_valid_image(path)
        )
        return min(
            images,
            key=lambda path: tuple(
                natural_key(part) for part in path.relative_to(album_dir).parts
            ),
            default=None,
        )
