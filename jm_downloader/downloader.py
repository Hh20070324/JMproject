import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jmcomic
from PIL import Image, UnidentifiedImageError

from .jmcomic_logging import install_safe_jmcomic_logging
from .pdf import (
    PART_FILE_MARKER,
    PdfPublishAborted,
    album_to_pdf,
    find_album_images,
    is_linked_directory,
    natural_key,
)
from .settings import AppPaths, DEFAULT_PATHS


LOGGER = logging.getLogger("jm-downloader")


class DownloadStopped(Exception):
    pass


class DownloadIntegrityError(Exception):
    pass


class ImageValidationError(DownloadIntegrityError):
    pass


class ManagedPathError(DownloadIntegrityError):
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
    ):
        self.album_id = str(album_id)
        self.on_progress = on_progress or (lambda *args: None)
        self.on_complete = on_complete or (lambda *args: None)
        self.on_error = on_error or (lambda *args: None)
        self.on_info = on_info or (lambda *args: None)
        self.on_preview = on_preview or (lambda *args: None)
        self.on_stopped = on_stopped or (lambda *args: None)
        self.paths = paths
        self.image_concurrency = max(1, int(image_concurrency))
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
        self.paths.ensure_output_directories()

    def _make_option(self):
        install_safe_jmcomic_logging()
        option = jmcomic.create_option_by_file(str(self.paths.option_file))
        option.download.threading.image = self.image_concurrency
        option.client.retry_times = self.REQUEST_RETRIES
        option.client.postman.meta_data.timeout = self.REQUEST_TIMEOUT_SECONDS
        return option

    def fetch_info(self):
        install_safe_jmcomic_logging()
        try:
            option = self._make_option()
            album = option.build_jm_client().get_album_detail(self.album_id)
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
                def __init__(self, active_option):
                    super().__init__(active_option)
                    owner._active_downloader = self

                def before_album(self, album):
                    super().before_album(album)
                    owner._total_photos = getattr(album, "page_count", 0) or 0
                    owner._album_total_known = owner._total_photos > 0
                    title = getattr(album, "title", None) or getattr(album, "name", None)
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

            jmcomic.download_album(
                self.album_id,
                option,
                downloader=ProgressDownloader,
                check_exception=False,
            )
            if self._stop_flag.is_set():
                return

            self._verify_download_result()

            self.on_progress(self.album_id, self.PDF_PCT, "打包 PDF", "")
            pdf_path = album_to_pdf(
                str(album_dir),
                str(self.paths.pdfs),
                publish_guard=lambda: not self._stop_flag.is_set(),
            )
            if not pdf_path:
                raise PdfPackagingError("PDF generator returned no output")
            if self._stop_flag.is_set():
                return
            self.on_complete(self.album_id, pdf_path)
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
            try:
                self.on_stopped(self.album_id)
            except Exception:
                LOGGER.exception(
                    "Download stopped callback failed for JM %s",
                    self.album_id,
                )

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

    @staticmethod
    def _public_error_message(error: Exception) -> str:
        if isinstance(error, ManagedPathError):
            return "下载路径未通过安全检查，请检查目录设置"
        if isinstance(error, (ImageValidationError, DownloadIntegrityError)):
            return "图片不完整或已损坏，请点击继续重试"
        if isinstance(error, PdfPackagingError):
            return "PDF 生成失败，图片已保留，可稍后继续"
        if isinstance(error, PermissionError):
            return "无法写入下载目录，请检查权限和磁盘空间"
        if isinstance(error, OSError):
            return "本地文件操作失败，请检查磁盘和下载目录"
        if isinstance(error, (ConnectionError, TimeoutError)):
            return "网络暂时不可用，请检查连接后继续"
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
