import os
import sys
import threading
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
os.chdir(str(PROJECT_ROOT))

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
    """

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

        # Output directories
        self.pictures_dir = PROJECT_ROOT / "Pictures"
        self.pdfs_dir = PROJECT_ROOT / "PDFs"
        self.pictures_dir.mkdir(parents=True, exist_ok=True)
        self.pdfs_dir.mkdir(parents=True, exist_ok=True)

    def fetch_info(self):
        """Fetch album title and cover before downloading. Returns (title, cover_url) or (None, None)."""
        try:
            option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
            client = option.build_jm_client()
            album = client.get_album_detail(self.album_id)
            title = album.title if hasattr(album, "title") else album.name
            cover_path = None
            try:
                cover_path = album.cover if hasattr(album, "cover") else None
            except Exception:
                cover_path = None
            return (title, cover_path)
        except Exception as e:
            return (None, None)

    def run(self):
        """Main download flow, designed to run in a thread."""
        try:
            # Step 1: Fetch info
            title, cover = self.fetch_info()
            self.on_info(self.album_id, title, cover)

            if self._stop_flag.is_set():
                return

            # Step 2: Configure option
            option = jmcomic.create_option_by_file(str(PROJECT_ROOT / "option.yml"))
            album_dir = self.pictures_dir / self.album_id
            album_dir.mkdir(parents=True, exist_ok=True)

            option.dir_rule.base_dir = str(self.pictures_dir)

            # Step 3: Download with progress callback
            downloaded_count = [0]

            def download_callback(photo, downloader):
                downloaded_count[0] += 1
                pct = min(99, 5 + downloaded_count[0] % 90)
                chapter = photo.from_album if hasattr(photo, "from_album") else ""
                if hasattr(chapter, "title"):
                    chapter = chapter.title
                self.on_progress(
                    self.album_id,
                    pct,
                    str(chapter) if chapter else "",
                    f"{downloaded_count[0]}",
                )

            jmcomic.download_album(
                self.album_id,
                option,
                callback=download_callback,
            )

            if self._stop_flag.is_set():
                return

            # Step 4: Pack PDF
            self.on_progress(self.album_id, 95, "打包 PDF", "")
            pdf_path = album_to_pdf(str(album_dir), str(self.pdfs_dir))
            self.on_complete(self.album_id, pdf_path)

        except Exception as e:
            traceback.print_exc()
            self.on_error(self.album_id, str(e))

    def start(self):
        """Start download in a background thread."""
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        """Request graceful stop."""
        self._stop_flag.set()
