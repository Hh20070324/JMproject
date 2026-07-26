"""v2.9.1 Phase 1: manifest schema v3 and offline chapter status."""

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from jm_downloader.downloader import DownloadWorker
from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestStore,
    CorruptChapterManifest,
    LibraryNotFound,
    LibraryService,
    UnsupportedChapterManifestVersion,
)
from jm_downloader.models import (
    ChapterImageStatus,
    ChapterManifest,
    ChapterManifestEntry,
    ChapterPackageStatus,
    LibraryLayout,
    TaskConfig,
)
from jm_downloader.packaging import chapter_to_cbz
from jm_downloader.pdf import chapter_to_pdf
from jm_downloader.settings import AppPaths


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color).save(path)
    return path


def _entry(
    photo_id: str,
    index: int,
    *,
    dir_name: str | None = None,
    page_count: int = 1,
    image_format: str | None = "jpg",
    downloaded_at_utc: str | None = "2026-01-01T00:00:00Z",
    package_format: str | None = None,
) -> ChapterManifestEntry:
    return ChapterManifestEntry(
        photo_id=photo_id,
        index=index,
        title=f"第 {index} 章",
        dir_name=f"第{index}章" if dir_name is None else dir_name,
        page_count=page_count,
        image_format=image_format,
        downloaded_at_utc=downloaded_at_utc,
        package_format=package_format,
    )


class ManifestV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.paths.pictures.mkdir(parents=True, exist_ok=True)
        self.store = ChapterManifestStore(self.paths)

    def _manifest(self, chapters) -> ChapterManifest:
        return ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=tuple(chapters),
        )

    def _write_raw(self, payload: dict) -> Path:
        path = self.paths.pictures / "123" / ".jm-chapters.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def test_current_schema_version_is_three(self):
        self.assertEqual(CHAPTER_MANIFEST_SCHEMA_VERSION, 3)

    def test_v3_round_trip_with_package_formats(self):
        chapters = [
            _entry("301", 1, package_format="pdf"),
            _entry("302", 2, package_format="cbz"),
            _entry("303", 3, package_format="images"),
        ]
        self.store.merge_and_save(self._manifest(chapters))

        loaded = self.store.load("123")

        self.assertEqual(
            [chapter.package_format for chapter in loaded.chapters],
            ["pdf", "cbz", "images"],
        )
        raw = json.loads(
            (self.paths.pictures / "123" / ".jm-chapters.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(raw["version"], 3)
        self.assertIn("package_format", raw["chapters"][0])

    def test_v1_and_v2_read_as_unknown_format_without_disk_rewrite(self):
        for version in (1, 2):
            chapter = {
                "photo_id": "301",
                "index": 1,
                "title": "第 1 章",
                "dir_name": "第1章",
                "page_count": 1,
            }
            if version >= 2:
                chapter.update(
                    {
                        "image_format": "jpg",
                        "downloaded_at_utc": "2026-01-01T00:00:00Z",
                    }
                )
            path = self._write_raw(
                {
                    "version": version,
                    "album_id": "123",
                    "album_title": "测试漫画",
                    "album_dir_name": "测试漫画",
                    "chapters": [chapter],
                }
            )
            before = path.read_bytes()

            manifest = self.store.load("123")

            self.assertEqual(manifest.version, 3)
            self.assertIsNone(manifest.chapters[0].package_format)
            self.assertEqual(path.read_bytes(), before)

    def test_future_version_still_rejected(self):
        self._write_raw(
            {
                "version": 4,
                "album_id": "123",
                "album_title": "测试漫画",
                "album_dir_name": "测试漫画",
                "chapters": [],
            }
        )
        with self.assertRaises(UnsupportedChapterManifestVersion):
            self.store.load("123")

    def test_invalid_package_format_rejected(self):
        manifest = self._manifest(
            [_entry("301", 1, package_format="epub")]
        )
        with self.assertRaises(CorruptChapterManifest):
            # merge_and_save raises ChapterManifestError; load wraps raw
            # decode failures as CorruptChapterManifest.  Validation errors
            # surface directly from the store API.
            try:
                self.store.merge_and_save(manifest)
            except Exception as error:
                raise CorruptChapterManifest(str(error)) from error

    def test_merge_keeps_other_chapters_formats(self):
        self.store.merge_and_save(
            self._manifest(
                [
                    _entry("301", 1, package_format="pdf"),
                    _entry("302", 2),
                ]
            )
        )
        incoming = self._manifest(
            [_entry("303", 3, package_format="cbz")]
        )

        merged = self.store.merge_and_save(incoming)

        self.assertEqual(
            [
                (chapter.photo_id, chapter.package_format)
                for chapter in merged.chapters
            ],
            [("301", "pdf"), ("302", None), ("303", "cbz")],
        )


class DownloadStampsPackageFormatTests(unittest.TestCase):
    def test_completed_download_marks_per_chapter_package_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            worker = DownloadWorker(
                "123",
                paths=paths,
                task_config=TaskConfig(package_format="cbz"),
                selected_chapter_ids=("301",),
            )
            worker._pending_manifest = ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=(
                    ChapterManifestEntry(
                        photo_id="301",
                        index=1,
                        title="第 1 章",
                        dir_name="第1章",
                        page_count=1,
                    ),
                ),
            )

            worker._mark_manifest_downloaded()

            (chapter,) = worker._pending_manifest.chapters
            self.assertEqual(chapter.package_format, "cbz")
            self.assertEqual(chapter.image_format, "jpg")
            self.assertIsNotNone(chapter.downloaded_at_utc)


class CheckChaptersTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.store = ChapterManifestStore(self.paths)

    def _save(self, chapters) -> None:
        self.store.merge_and_save(
            ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=tuple(chapters),
            )
        )

    def _chapter_dir(self, name: str = "第1章") -> Path:
        return self.paths.pictures / "123" / "测试漫画" / name

    def _package_dir(self) -> Path:
        path = self.paths.pdfs / "123" / "测试漫画"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_complete_pdf_chapter(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        _write_image(chapter_dir / "2.jpg", "blue")
        chapter_to_pdf(chapter_dir, self._package_dir() / "第1章.pdf")
        self._save([_entry("301", 1, page_count=2, package_format="pdf")])

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.COMPLETE)
        self.assertEqual(snapshot.package_status, ChapterPackageStatus.COMPLETE)
        self.assertEqual(snapshot.package_format, "pdf")
        self.assertEqual(snapshot.valid_image_count, 2)
        self.assertEqual(snapshot.problem_codes, ())
        self.assertFalse(snapshot.can_rebuild)
        self.assertFalse(snapshot.can_redownload)
        self.assertTrue(snapshot.can_delete_images)
        self.assertTrue(snapshot.can_delete_package)
        self.assertTrue(snapshot.can_delete_all)

    def test_missing_chapter_directory(self):
        self._save([_entry("301", 1, page_count=2, package_format="pdf")])

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.MISSING)
        self.assertEqual(snapshot.package_status, ChapterPackageStatus.MISSING)
        self.assertIn("images_missing", snapshot.problem_codes)
        self.assertIn("package_missing", snapshot.problem_codes)
        self.assertFalse(snapshot.can_rebuild)
        self.assertTrue(snapshot.can_redownload)
        self.assertFalse(snapshot.can_delete_images)

    def test_short_image_count_is_missing(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        self._save([_entry("301", 1, page_count=2, package_format="images")])

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.MISSING)
        self.assertEqual(
            snapshot.package_status,
            ChapterPackageStatus.NOT_APPLICABLE,
        )
        self.assertFalse(snapshot.can_delete_package)

    def test_undecodable_and_extra_images_are_damaged(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        (chapter_dir / "2.jpg").write_bytes(b"not an image")
        self._save([_entry("301", 1, page_count=2, package_format="images")])

        (snapshot,) = self.service.check_chapters("123")
        self.assertEqual(snapshot.image_status, ChapterImageStatus.DAMAGED)
        self.assertIn("images_damaged", snapshot.problem_codes)

        extra_dir = self._chapter_dir("第2章")
        _write_image(extra_dir / "1.jpg")
        _write_image(extra_dir / "2.jpg", "blue")
        _write_image(extra_dir / "3.jpg", "red")
        self._save(
            [
                _entry("301", 1, page_count=2, package_format="images"),
                _entry("302", 2, page_count=2, package_format="images"),
            ]
        )
        snapshots = self.service.check_chapters("123")
        self.assertEqual(snapshots[1].image_status, ChapterImageStatus.DAMAGED)

    def test_wrong_extension_is_damaged(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.png")
        self._save(
            [
                _entry(
                    "301",
                    1,
                    page_count=1,
                    image_format="jpg",
                    package_format="images",
                )
            ]
        )

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.DAMAGED)

    def test_damaged_pdf_and_cbz_count_mismatch(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        _write_image(chapter_dir / "2.jpg", "blue")
        package_dir = self._package_dir()
        (package_dir / "第1章.pdf").write_bytes(b"%PDF-broken")
        chapter_to_cbz(chapter_dir, package_dir / "第2章.cbz")
        chapter2_dir = self._chapter_dir("第2章")
        _write_image(chapter2_dir / "1.jpg")
        _write_image(chapter2_dir / "2.jpg", "blue")
        _write_image(chapter2_dir / "3.jpg", "red")
        self._save(
            [
                _entry("301", 1, page_count=2, package_format="pdf"),
                _entry("302", 2, page_count=3, package_format="cbz"),
            ]
        )

        first, second = self.service.check_chapters("123")

        self.assertEqual(first.package_status, ChapterPackageStatus.DAMAGED)
        self.assertTrue(first.can_rebuild)
        self.assertEqual(second.package_status, ChapterPackageStatus.DAMAGED)
        self.assertTrue(second.can_rebuild)

    def test_missing_package_with_complete_images_allows_rebuild(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        self._save([_entry("301", 1, page_count=1, package_format="cbz")])

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.COMPLETE)
        self.assertEqual(snapshot.package_status, ChapterPackageStatus.MISSING)
        self.assertTrue(snapshot.can_rebuild)
        self.assertFalse(snapshot.can_redownload)

    def test_unknown_format_waits_for_confirmation_and_suggests(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        package_dir = self._package_dir()
        chapter_to_pdf(chapter_dir, package_dir / "第1章.pdf")
        chapter_to_cbz(chapter_dir, package_dir / "第2章.cbz")
        chapter2_dir = self._chapter_dir("第2章")
        _write_image(chapter2_dir / "1.jpg")
        both_dir = self._chapter_dir("第3章")
        _write_image(both_dir / "1.jpg")
        chapter_to_pdf(both_dir, package_dir / "第3章.pdf")
        chapter_to_cbz(both_dir, package_dir / "第3章.cbz")
        self._save(
            [
                _entry("301", 1, page_count=1),
                _entry("302", 2, page_count=1),
                _entry("303", 3, page_count=1),
                _entry("304", 4, page_count=1),
            ]
        )

        snapshots = self.service.check_chapters("123")

        self.assertEqual(
            [s.package_status for s in snapshots],
            [ChapterPackageStatus.UNKNOWN] * 4,
        )
        self.assertEqual(snapshots[0].suggested_package_format, "pdf")
        self.assertEqual(snapshots[1].suggested_package_format, "cbz")
        self.assertIsNone(snapshots[2].suggested_package_format)
        self.assertIsNone(snapshots[3].suggested_package_format)
        self.assertTrue(
            all("format_unknown" in s.problem_codes for s in snapshots)
        )
        self.assertFalse(any(s.can_rebuild for s in snapshots))
        # Both exact artifacts of chapter 3 exist: they may be deleted.
        self.assertTrue(snapshots[2].can_delete_package)
        self.assertFalse(snapshots[3].can_delete_package)

    def test_check_does_not_write_manifest(self):
        self._save([_entry("301", 1, page_count=1)])
        path = self.paths.pictures / "123" / ".jm-chapters.json"
        before = path.read_bytes()

        self.service.check_chapters("123")

        self.assertEqual(path.read_bytes(), before)

    def test_manifest_only_album_stays_visible(self):
        self._save(
            [
                _entry("301", 1, page_count=1, package_format="pdf"),
                _entry("302", 2, page_count=1, package_format="images"),
            ]
        )

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertFalse(item.has_images)
        self.assertFalse(item.has_pdf)
        self.assertEqual(item.chapter_count, 2)
        self.assertEqual(item.title, "测试漫画")

    def test_check_requires_manifest(self):
        with self.assertRaises(LibraryNotFound):
            self.service.check_chapters("999")

    def test_single_chapter_direct_layout(self):
        title_dir = self.paths.pictures / "123" / "测试漫画"
        _write_image(title_dir / "1.jpg")
        chapter_to_pdf(title_dir, self._package_dir() / "测试漫画.pdf")
        self._save(
            [
                _entry(
                    "301",
                    1,
                    dir_name="",
                    page_count=1,
                    package_format="pdf",
                )
            ]
        )

        (snapshot,) = self.service.check_chapters("123")

        self.assertEqual(snapshot.image_status, ChapterImageStatus.COMPLETE)
        self.assertEqual(snapshot.package_status, ChapterPackageStatus.COMPLETE)
        self.assertEqual(
            snapshot.package_path,
            (self.paths.pdfs / "123" / "测试漫画" / "测试漫画.pdf").resolve(),
        )

    def test_chapter_failure_does_not_hide_other_chapters(self):
        chapter_dir = self._chapter_dir()
        _write_image(chapter_dir / "1.jpg")
        self._save(
            [
                _entry("301", 1, page_count=1, package_format="images"),
                _entry("302", 2, page_count=1, package_format="images"),
            ]
        )
        # Chapter 2 directory is replaced by a regular file.
        blocker = self.paths.pictures / "123" / "测试漫画" / "第2章"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_bytes(b"not a directory")

        snapshots = self.service.check_chapters("123")

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0].image_status, ChapterImageStatus.COMPLETE)
        self.assertEqual(snapshots[1].image_status, ChapterImageStatus.MISSING)


if __name__ == "__main__":
    unittest.main()
