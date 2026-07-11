import threading
from pathlib import Path

import jmcomic

from .pdf import album_to_pdf, natural_key
from .settings import AppPaths, DEFAULT_PATHS


class DownloadWorker:
    PDF_PCT = 95

    def __init__(
        self,
        album_id: str,
        on_progress=None,
        on_complete=None,
        on_error=None,
        on_info=None,
        on_preview=None,
        paths: AppPaths = DEFAULT_PATHS,
    ):
        self.album_id = str(album_id)
        self.on_progress = on_progress or (lambda *args: None)
        self.on_complete = on_complete or (lambda *args: None)
        self.on_error = on_error or (lambda *args: None)
        self.on_info = on_info or (lambda *args: None)
        self.on_preview = on_preview or (lambda *args: None)
        self.paths = paths
        self._stop_flag = threading.Event()
        self._thread = None
        self._total_photos = 0
        self._album_total_known = False
        self._downloaded_count = 0
        self._progress_lock = threading.Lock()
        self._preview_path = None
        self.paths.ensure_output_directories()

    def _make_option(self):
        return jmcomic.create_option_by_file(str(self.paths.option_file))

    def fetch_info(self):
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
            option = self._make_option()
            album_dir = self.paths.pictures / self.album_id
            album_dir.mkdir(parents=True, exist_ok=True)
            option.dir_rule.base_dir = str(self.paths.pictures)
            owner = self

            class ProgressDownloader(jmcomic.JmDownloader):
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

                def before_image(self, image, image_path):
                    super().before_image(image, image_path)
                    if image.cache and image.exists:
                        owner._on_image_ready(image, image_path)

                def after_image(self, image, image_path):
                    super().after_image(image, image_path)
                    owner._on_image_ready(image, image_path)

            jmcomic.download_album(self.album_id, option, downloader=ProgressDownloader)
            if self._stop_flag.is_set():
                return

            self.on_progress(self.album_id, self.PDF_PCT, "打包 PDF", "")
            pdf_path = album_to_pdf(str(album_dir), str(self.paths.pdfs))
            self.on_complete(self.album_id, pdf_path)
        except Exception as error:
            self.on_error(self.album_id, str(error))

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop_flag.set()

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

            candidate = self._find_first_downloaded_image()
            if candidate is not None and candidate != self._preview_path:
                self._preview_path = candidate
                self.on_preview(self.album_id, str(candidate))

    def _find_first_downloaded_image(self) -> Path | None:
        album_dir = self.paths.pictures / self.album_id
        images = (
            path
            for path in album_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")
        )
        return min(
            images,
            key=lambda path: tuple(
                natural_key(part) for part in path.relative_to(album_dir).parts
            ),
            default=None,
        )
