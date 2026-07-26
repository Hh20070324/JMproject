"""v2.9.1 Phase 3: staged chapter-level deletion transactions."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestStore,
    LibraryError,
    LibraryService,
)
from jm_downloader.models import (
    ChapterImageStatus,
    ChapterManifest,
    ChapterManifestEntry,
    LibraryLayout,
)
from jm_downloader.settings import AppPaths


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 4), color).save(path)
    return path


def _entry(
    photo_id: str,
    index: int,
    *,
    package_format: str = "pdf",
    dir_name: str | None = None,
) -> ChapterManifestEntry:
    return ChapterManifestEntry(
        photo_id=photo_id,
        index=index,
        title=f"第 {index} 章",
        dir_name=f"第{index}章" if dir_name is None else dir_name,
        page_count=1,
        image_format="jpg",
        downloaded_at_utc="2026-07-26T00:00:00Z",
        package_format=package_format,
    )


class ChapterDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.store = ChapterManifestStore(self.paths)

    def _save(self, chapters) -> Path:
        self.store.merge_and_save(
            ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=tuple(chapters),
            )
        )
        return self.paths.pictures / "123" / ".jm-chapters.json"

    def _image(self, chapter: str, color: str = "green") -> Path:
        return _write_image(
            self.paths.pictures
            / "123"
            / "测试漫画"
            / chapter
            / "1.jpg",
            color,
        )

    def _package(self, name: str, payload: bytes = b"package") -> Path:
        path = self.paths.pdfs / "123" / "测试漫画" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_album_delete_images_preserves_all_manifest_entries(self):
        first = self._image("第1章")
        second = self._image("第2章", "blue")
        self._save(
            [
                _entry("301", 1),
                _entry("302", 2, package_format="cbz"),
            ]
        )

        self.service.delete_images("123")

        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        manifest = self.store.load("123")
        self.assertEqual(
            [chapter.photo_id for chapter in manifest.chapters],
            ["301", "302"],
        )
        item = self.service.get_item("123")
        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertEqual(item.chapter_count, 2)
        self.assertFalse(item.has_images)
        self.assertEqual(
            [status.image_status for status in self.service.check_chapters("123")],
            [ChapterImageStatus.MISSING, ChapterImageStatus.MISSING],
        )

    def test_delete_chapter_images_keeps_manifest_package_and_extra_file(self):
        image = self._image("第1章")
        extra = image.parent / "notes.txt"
        extra.write_text("keep", encoding="utf-8")
        package = self._package("第1章.pdf")
        self._save([_entry("301", 1)])
        expected = self.service.check_chapters("123")[0]

        result = self.service.delete_chapter(
            "123",
            "301",
            "images",
            expected=expected,
        )

        self.assertEqual(result.deleted_image_count, 1)
        self.assertFalse(result.album_removed)
        self.assertFalse(image.exists())
        self.assertTrue(extra.is_file())
        self.assertTrue(package.is_file())
        self.assertEqual(len(self.store.load("123").chapters), 1)

    def test_delete_package_removes_pdf_and_cbz_only(self):
        image = self._image("第1章")
        pdf = self._package("第1章.pdf", b"pdf")
        cbz = self._package("第1章.cbz", b"cbz")
        self._save([_entry("301", 1)])

        result = self.service.delete_chapter(
            "123",
            "301",
            "package",
        )

        self.assertEqual(result.deleted_package_count, 2)
        self.assertTrue(image.is_file())
        self.assertFalse(pdf.exists())
        self.assertFalse(cbz.exists())
        self.assertEqual(
            self.store.load("123").chapters[0].package_format,
            "pdf",
        )

    def test_images_only_chapter_rejects_package_delete(self):
        self._image("第1章")
        package = self._package("第1章.pdf")
        self._save([_entry("301", 1, package_format="images")])

        with self.assertRaisesRegex(LibraryError, "仅图片"):
            self.service.delete_chapter("123", "301", "package")

        self.assertTrue(package.is_file())

    def test_delete_all_removes_only_selected_chapter_and_manifest_entry(self):
        first_image = self._image("第1章")
        second_image = self._image("第2章", "blue")
        first_pdf = self._package("第1章.pdf")
        second_pdf = self._package("第2章.pdf")
        self._save([_entry("301", 1), _entry("302", 2)])

        result = self.service.delete_chapter("123", "301", "all")

        self.assertFalse(result.album_removed)
        self.assertFalse(first_image.exists())
        self.assertFalse(first_pdf.exists())
        self.assertTrue(second_image.is_file())
        self.assertTrue(second_pdf.is_file())
        self.assertEqual(
            [chapter.photo_id for chapter in self.store.load("123").chapters],
            ["302"],
        )

    def test_delete_last_chapter_cleans_empty_managed_roots_only(self):
        image = self._image("第1章")
        package = self._package("第1章.pdf")
        flat_pdf = self.paths.pdfs / "123.pdf"
        flat_pdf.write_bytes(b"keep")
        self._save([_entry("301", 1)])

        result = self.service.delete_chapter("123", "301", "all")

        self.assertTrue(result.album_removed)
        self.assertFalse(image.exists())
        self.assertFalse(package.exists())
        self.assertFalse((self.paths.pictures / "123").exists())
        self.assertFalse((self.paths.pdfs / "123").exists())
        self.assertEqual(flat_pdf.read_bytes(), b"keep")

    def test_manifest_write_failure_restores_staged_images(self):
        image = self._image("第1章")
        manifest_path = self._save([_entry("301", 1)])
        before = manifest_path.read_bytes()

        with patch.object(
            ChapterManifestStore,
            "replace_exact",
            side_effect=PermissionError("locked"),
        ):
            with self.assertRaisesRegex(LibraryError, "删除章节失败"):
                self.service.delete_chapter(
                    "123",
                    "301",
                    "images",
                )

        self.assertTrue(image.is_file())
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(
            list(image.parent.glob(".*.chapter-images.delete")),
            [],
        )

    def test_stale_snapshot_rejects_delete_without_changes(self):
        image = self._image("第1章")
        self._save([_entry("301", 1)])
        expected = self.service.check_chapters("123")[0]
        _write_image(image.parent / "2.jpg", "blue")

        with self.assertRaisesRegex(LibraryError, "发生变化"):
            self.service.delete_chapter(
                "123",
                "301",
                "images",
                expected=expected,
            )

        self.assertTrue(image.is_file())
        self.assertTrue((image.parent / "2.jpg").is_file())

    def test_linked_package_is_rejected_without_touching_target(self):
        self._image("第1章")
        self._save([_entry("301", 1)])
        outside = self.paths.root / "outside.pdf"
        outside.write_bytes(b"outside")
        link = self.paths.pdfs / "123" / "测试漫画" / "第1章.pdf"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"当前账户不能创建符号链接: {error}")

        with self.assertRaisesRegex(LibraryError, "链接"):
            self.service.delete_chapter("123", "301", "all")

        self.assertEqual(outside.read_bytes(), b"outside")

    def test_linked_image_is_rejected_without_touching_target(self):
        image = self._image("第1章")
        self._save([_entry("301", 1)])
        outside = self.paths.root / "outside.jpg"
        outside.write_bytes(b"outside")
        link = image.parent / "2.jpg"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"当前账户不能创建符号链接: {error}")

        with self.assertRaisesRegex(LibraryError, "链接"):
            self.service.delete_chapter("123", "301", "images")

        self.assertTrue(image.is_file())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_mid_stage_failure_rolls_back_earlier_files(self):
        image = self._image("第1章")
        package = self._package("第1章.pdf")
        self._save([_entry("301", 1)])
        original_replace = os.replace

        def fail_package(source, destination):
            if Path(source) == package:
                raise PermissionError("locked")
            return original_replace(source, destination)

        with patch(
            "jm_downloader.library.os.replace",
            side_effect=fail_package,
        ):
            with self.assertRaisesRegex(LibraryError, "删除章节失败"):
                self.service.delete_chapter("123", "301", "all")

        self.assertTrue(image.is_file())
        self.assertTrue(package.is_file())
        self.assertEqual(len(self.store.load("123").chapters), 1)


if __name__ == "__main__":
    unittest.main()
