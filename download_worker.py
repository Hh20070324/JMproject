import threading
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()

import jmcomic
from jpg2pdf import album_to_pdf


class DownloadWorker:
    """
    Wraps jmcomic download + PDF packing into a thread with progress callback.

    Callbacks:
        on_progress(album_id, percent, chapter, page_info)
        on_complete(album_id, pdf_path)
        on_error(album_id, error_message)
        on_info(album_id, title, cover_url)

    Note: stop() sets a flag checked between download phases, but cannot
    interrupt an in-progress jmcomic.download_album() call mid-stream.
    """

    PDF_PCT = 95  # progress value indicating "packing PDF" phase

    def __init__(
        self,
        album_id: str,
        on_progress=None,
        on_complete=None,
        on_error=None,
        on_info=None,
    ):
        self.album_id = str(album_id)
        self.on_progress = on_progress or (lambda *a: None)
        self.on_complete = on_complete or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)
        self.on_info = on_info or (lambda *a: None)
        self._stop_flag = threading.Event()
        self._thread = None
        self._total_photos = 0

        # Output directories
        self.pictures_dir = PROJECT_ROOT / "Pictures"
        self.pdfs_dir = PROJECT_ROOT / "PDFs"
        self.pictures_dir.mkdir(parents=True, exist_ok=True)
        self.pdfs_dir.mkdir(parents=True, exist_ok=True)

    def _make_option(self):
        """Create a jmcomic option from the project's option.yml."""
        return jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))

    def fetch_info(self):
        """
        Fetch album title, cover URL, and total photo count.
        Returns (title, cover_url, total_photos) or (None, None, 0) on failure.
        """
        try:
            option = self._make_option()
            client = option.build_jm_client()
            album = client.get_album_detail(self.album_id)
            title = album.title if hasattr(album, "title") else album.name
            cover_url = album.cover if hasattr(album, "cover") else None
            total = len(album) if hasattr(album, "__len__") else 0
            return (title, cover_url, total)
        except Exception:
            return (None, None, 0)

    def run(self):
        """Main download flow, designed to run in a thread."""
        try:
            # Step 1: Fetch info
            title, cover, total = self.fetch_info()
            self._total_photos = total
            self.on_info(self.album_id, title, cover)

            if self._stop_flag.is_set():
                return

            # Step 2: Configure option
            option = self._make_option()
            album_dir = self.pictures_dir / self.album_id
            album_dir.mkdir(parents=True, exist_ok=True)

            option.dir_rule.base_dir = str(self.pictures_dir)

            # Step 3: Download with progress callback
            downloaded_count = 0

            def download_callback(photo, downloader):
                nonlocal downloaded_count
                downloaded_count += 1
                if self._total_photos > 0:
                    pct = min(94, int(downloaded_count / self._total_photos * 94))
                else:
                    pct = min(94, 5 + int(downloaded_count / 40 * 89))

                chapter = ""
                if hasattr(photo, "from_album") and photo.from_album:
                    chapter = getattr(photo.from_album, "title", "") or ""
                self.on_progress(
                    self.album_id,
                    pct,
                    str(chapter),
                    f"{downloaded_count}/{self._total_photos or '?'}",
                )

            jmcomic.download_album(
                self.album_id,
                option,
                callback=download_callback,
            )

            if self._stop_flag.is_set():
                return

            # Step 4: Pack PDF
            self.on_progress(self.album_id, self.PDF_PCT, "打包 PDF", "")
            pdf_path = album_to_pdf(str(album_dir), str(self.pdfs_dir))
            self.on_complete(self.album_id, pdf_path)

        except Exception as e:
            self.on_error(self.album_id, str(e))

    def start(self):
        """Start download in a background thread."""
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        """Request graceful stop (checked between phases, not mid-download)."""
        self._stop_flag.set()
