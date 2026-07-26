import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestStore,
    LibraryService,
)
from jm_downloader.models import ChapterManifest, ChapterManifestEntry
from jm_downloader.settings import AppPaths


class Phase7LibraryTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_manifest_uses_latest_chapter_download_time(self):
        chapter_one = self.paths.pictures / "123" / "漫画" / "第1章"
        chapter_two = self.paths.pictures / "123" / "漫画" / "第2章"
        chapter_one.mkdir(parents=True)
        chapter_two.mkdir(parents=True)
        Image.new("RGB", (2, 2), "green").save(chapter_one / "1.jpg")
        Image.new("RGB", (2, 2), "green").save(chapter_two / "1.jpg")
        ChapterManifestStore(self.paths).merge_and_save(
            ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="漫画",
                album_dir_name="漫画",
                chapters=(
                    ChapterManifestEntry(
                        "301",
                        1,
                        "第一章",
                        "第1章",
                        1,
                        image_format="jpg",
                        downloaded_at_utc="2026-07-20T00:00:00Z",
                    ),
                    ChapterManifestEntry(
                        "302",
                        2,
                        "第二章",
                        "第2章",
                        1,
                        image_format="jpg",
                        downloaded_at_utc="2026-07-22T00:00:00Z",
                    ),
                ),
            )
        )

        item = self.service.get_item("123")

        self.assertEqual(
            item.downloaded_at_utc,
            "2026-07-22T00:00:00Z",
        )

    def test_old_layout_falls_back_to_album_directory_mtime(self):
        album_dir = self.paths.pictures / "456"
        chapter = album_dir / "旧章节"
        chapter.mkdir(parents=True)
        Image.new("RGB", (2, 2), "green").save(chapter / "1.jpg")
        timestamp = 1_700_000_000.25
        os.utime(album_dir, (timestamp, timestamp))

        item = self.service.get_item("456")

        self.assertIsNotNone(item.downloaded_at_utc)
        self.assertTrue(item.downloaded_at_utc.endswith("Z"))
        self.assertEqual(
            item.downloaded_at_utc,
            LibraryService._directory_downloaded_at(album_dir),
        )

    def test_batch_safe_service_never_touches_flat_legacy_pdf(self):
        album_dir = self.paths.pictures / "789" / "旧章节"
        album_dir.mkdir(parents=True)
        Image.new("RGB", (2, 2), "green").save(album_dir / "1.jpg")
        flat_pdf = self.paths.pdfs / "789.pdf"
        flat_pdf.write_bytes(b"legacy-whole-album")

        self.service.delete_all("789")

        self.assertEqual(flat_pdf.read_bytes(), b"legacy-whole-album")
        self.assertFalse((self.paths.pictures / "789").exists())


if __name__ == "__main__":
    unittest.main()
