import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PIL import Image

from jm_downloader.library import ChapterManifestStore, LibraryService
from jm_downloader.models import (
    ChapterImageStatus,
    ChapterManifest,
    ChapterManifestEntry,
    ReaderContentMode,
    ReaderHistoryEntry,
    ReaderSource,
)
from jm_downloader.qt.widgets.reader_graphics_view import (
    MAX_DECODE_TARGET_WIDTH,
    PAGE_GAP,
    ReaderGraphicsView,
)
from jm_downloader.settings import AppPaths


class V32Phase0ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_reader_content_mode_is_separate_from_entry_source(self):
        entry = ReaderHistoryEntry(
            album_id="123",
            title="测试漫画",
            photo_id="301",
            chapter_title="第 1 章",
            chapter_index=1,
            page_number=2,
            page_count=5,
            read_at_utc="2026-07-29T00:00:00Z",
            source=ReaderSource.LOCAL_LIBRARY,
            content_mode=ReaderContentMode.LOCAL,
        )

        self.assertEqual(entry.source, ReaderSource.LOCAL_LIBRARY)
        self.assertEqual(entry.content_mode, ReaderContentMode.LOCAL)
        with self.assertRaises(FrozenInstanceError):
            entry.content_mode = ReaderContentMode.ONLINE

    def test_existing_history_construction_defaults_to_online(self):
        entry = ReaderHistoryEntry(
            album_id="123",
            title="测试漫画",
            photo_id="301",
            chapter_title="第 1 章",
            chapter_index=1,
            page_number=1,
            page_count=1,
            read_at_utc="2026-07-29T00:00:00Z",
            source=ReaderSource.SEARCH,
        )

        self.assertEqual(entry.content_mode, ReaderContentMode.ONLINE)

    def test_managed_complete_chapter_exposes_only_controlled_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths(Path(temporary))
            chapter_dir = paths.pictures / "123" / "测试漫画" / "第1章"
            chapter_dir.mkdir(parents=True)
            Image.new("RGB", (8, 8), "white").save(
                chapter_dir / "1.jpg",
                format="JPEG",
            )
            manifest = ChapterManifest(
                version=2,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=(
                    ChapterManifestEntry(
                        photo_id="301",
                        index=1,
                        title="第1章",
                        dir_name="第1章",
                        page_count=1,
                        image_format="jpg",
                        package_format="images",
                    ),
                ),
            )
            ChapterManifestStore(paths).replace_exact(manifest)

            chapters = LibraryService(paths).check_chapters("123")

            self.assertEqual(len(chapters), 1)
            self.assertEqual(
                chapters[0].image_status,
                ChapterImageStatus.COMPLETE,
            )
            self.assertEqual(chapters[0].image_directory, chapter_dir)
            self.assertTrue(
                chapters[0].image_directory.is_relative_to(paths.pictures)
            )

    def test_reader_geometry_keeps_zero_gap_and_bounded_decode_target(self):
        view = ReaderGraphicsView()
        view.resize(1200, 800)
        view.show()
        self.app.processEvents()
        view.set_zoom_percent(25)

        self.assertEqual(PAGE_GAP, 0)
        self.assertGreaterEqual(view.target_width, view.content_width)
        self.assertLessEqual(view.target_width, MAX_DECODE_TARGET_WIDTH)
        view.close()


if __name__ == "__main__":
    unittest.main()
