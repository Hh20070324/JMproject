import tempfile
import unittest
from pathlib import Path

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_FILENAME,
    ChapterManifestStore,
    LibraryError,
    LibraryNotFound,
    LibraryService,
)
from jm_downloader.models import (
    ChapterImageStatus,
    ChapterManifest,
    ChapterManifestEntry,
)
from jm_downloader.settings import AppPaths


class V322LocalReadBaselineContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.service = LibraryService(self.paths)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_manifest(self, *, page_count: int = 1) -> Path:
        ChapterManifestStore(self.paths).replace_exact(
            ChapterManifest(
                version=3,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=(
                    ChapterManifestEntry(
                        photo_id="301",
                        index=1,
                        title="第 1 章",
                        dir_name="第1章",
                        page_count=page_count,
                        image_format="jpg",
                        package_format="images",
                    ),
                ),
            )
        )
        return self.paths.pictures / "123" / CHAPTER_MANIFEST_FILENAME

    def test_missing_manifest_is_distinct_from_unreadable_local_content(self):
        with self.assertRaisesRegex(LibraryNotFound, "没有可用的章节清单"):
            self.service.check_chapters("123")

    def test_complete_manifest_chapter_is_available_for_local_read(self):
        image_path = (
            self.paths.pictures
            / "123"
            / "测试漫画"
            / "第1章"
            / "1.jpg"
        )
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (12, 12), "white").save(
            image_path,
            format="JPEG",
        )
        self._write_manifest()

        (chapter,) = self.service.check_chapters("123")

        self.assertEqual(chapter.image_status, ChapterImageStatus.COMPLETE)
        self.assertEqual(chapter.photo_id, "301")
        self.assertTrue(chapter.image_directory.is_relative_to(self.paths.pictures))

    def test_existing_manifest_without_images_is_not_normal_absence(self):
        self._write_manifest()

        (chapter,) = self.service.check_chapters("123")

        self.assertEqual(chapter.image_status, ChapterImageStatus.MISSING)
        self.assertNotIn("check_error", chapter.problem_codes)

    def test_corrupt_manifest_is_a_check_failure_not_normal_absence(self):
        manifest_path = self._write_manifest()
        manifest_path.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(LibraryError, "章节清单不可用"):
            self.service.check_chapters("123")


if __name__ == "__main__":
    unittest.main()
