import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from jm_downloader.library import (
    CHAPTER_MANIFEST_FILENAME,
    ChapterManifestStore,
    LibraryError,
    LibraryNotFound,
    LibraryService,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    LibraryItem,
    LibraryLayout,
)
from jm_downloader.settings import AppPaths


class V28LibraryLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.manifests = ChapterManifestStore(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_library_item_exposes_explicit_layout_and_directory_semantics(self):
        self.assertEqual(
            {value.value for value in LibraryLayout},
            {"managed", "legacy", "unverified"},
        )
        names = [field.name for field in fields(LibraryItem)]
        self.assertIn("title", names)
        self.assertIn("layout", names)
        self.assertIn("pdf_directory", names)
        self.assertNotIn("pdf_path", names)

    def test_valid_manifest_pins_title_chapter_order_and_pdf_directory(self):
        self.manifests.merge_and_save(self._manifest())
        late = self._write(
            "Pictures/123/固定目录/第2章/10.jpg",
            b"late",
        )
        first = self._write(
            "Pictures/123/固定目录/第1章/2.jpg",
            b"first",
        )
        self._write("PDFs/123/固定目录/第2章.pdf", b"pdf")

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertEqual(item.title, "远端原始标题")
        self.assertEqual(item.chapter_count, 2)
        self.assertEqual(item.image_count, 2)
        self.assertEqual(item.preview_path, first.resolve())
        self.assertNotEqual(item.preview_path, late.resolve())
        self.assertEqual(
            item.pdf_directory,
            (self.paths.pdfs / "123" / "固定目录").resolve(),
        )

    def test_manifest_path_drift_is_not_guessed_and_part_files_are_excluded(self):
        self.manifests.merge_and_save(self._manifest())
        self._write("Pictures/123/固定目录/第1章/1.jpg", b"image")
        self._write("PDFs/123/远端改名/第1章.pdf", b"wrong")
        self._write(
            "PDFs/123/固定目录/.jm-part-stale.pdf",
            b"partial",
        )
        self._write("PDFs/123/固定目录/第1章.pdf.part", b"partial")
        self._write("PDFs/999/未知/.jm-part-stale.pdf", b"partial")

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertFalse(item.has_pdf)
        self.assertIsNone(item.pdf_directory)
        self.assertNotIn("999", {value.album_id for value in self.service.list_items()})

    def test_image_layout_drift_degrades_to_legacy_without_guessing_title(self):
        self.manifests.merge_and_save(self._manifest())
        image = self._write(
            "Pictures/123/用户改名目录/第1章/1.jpg",
            b"image",
        )

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.LEGACY)
        self.assertIsNone(item.title)
        self.assertEqual(item.preview_path, image.resolve())

    def test_pdf_layout_drift_degrades_to_unverified_album_root(self):
        self.manifests.merge_and_save(self._manifest())
        self._write("PDFs/123/用户改名目录/第1章.pdf", b"pdf")

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.UNVERIFIED)
        self.assertIsNone(item.title)
        self.assertEqual(
            item.pdf_directory,
            (self.paths.pdfs / "123").resolve(),
        )

    def test_corrupt_manifest_never_supplies_title_or_managed_paths(self):
        manifest_path = (
            self.paths.pictures / "123" / CHAPTER_MANIFEST_FILENAME
        )
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(b"{broken")
        self._write("Pictures/123/任意旧目录/1.jpg", b"image")
        self._write("PDFs/123/任意目录/第1章.pdf", b"pdf")

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.LEGACY)
        self.assertIsNone(item.title)
        self.assertFalse(item.has_pdf)

        self.service.delete_images("123")
        item = self.service.get_item("123")
        self.assertEqual(item.layout, LibraryLayout.UNVERIFIED)
        self.assertEqual(item.pdf_directory, (self.paths.pdfs / "123").resolve())

    def test_future_manifest_is_not_downgraded_to_destructive_legacy_layout(self):
        manifest_path = (
            self.paths.pictures / "123" / CHAPTER_MANIFEST_FILENAME
        )
        manifest_path.parent.mkdir(parents=True)
        payload = {
            "version": 2,
            "album_id": "123",
            "album_title": "未来标题",
            "album_dir_name": "未来目录",
            "chapters": [],
        }
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        image = self._write("Pictures/123/未来目录/1.jpg", b"image")

        self.assertEqual(self.service.list_items(), [])
        with self.assertRaises(LibraryNotFound):
            self.service.delete_images("123")
        self.assertEqual(image.read_bytes(), b"image")

    def test_managed_delete_images_rolls_back_when_minimal_manifest_fails(self):
        original = self.manifests.merge_and_save(self._manifest())
        image = self._write(
            "Pictures/123/固定目录/第1章/1.jpg",
            b"image",
        )
        before = (
            self.paths.pictures / "123" / CHAPTER_MANIFEST_FILENAME
        ).read_bytes()

        with patch.object(
            ChapterManifestStore,
            "replace_exact",
            side_effect=PermissionError("locked"),
        ):
            with self.assertRaisesRegex(LibraryError, "删除图片失败"):
                self.service.delete_images("123")

        self.assertEqual(image.read_bytes(), b"image")
        self.assertEqual(
            (
                self.paths.pictures / "123" / CHAPTER_MANIFEST_FILENAME
            ).read_bytes(),
            before,
        )
        self.assertEqual(self.manifests.load("123"), original)
        self.assertEqual(
            list(self.paths.pictures.glob(".*.images.delete")),
            [],
        )

    def test_unmanaged_flat_pdf_is_never_opened_or_deleted(self):
        whole = self._write("PDFs/123.pdf", b"keep")

        self.assertEqual(self.service.list_items(), [])
        with self.assertRaises(LibraryNotFound):
            self.service.open_location("123", "pdf")
        with self.assertRaises(LibraryNotFound):
            self.service.delete_pdf("123")
        with self.assertRaises(LibraryNotFound):
            self.service.delete_all("123")
        self.assertEqual(whole.read_bytes(), b"keep")

    def test_linked_descendant_blocks_delete_without_touching_outside_file(self):
        self._write("Pictures/123/旧章节/1.jpg", b"image")
        outside = self.paths.root / "outside.txt"
        outside.write_bytes(b"outside")
        link = self.paths.pictures / "123" / "旧章节" / "outside-link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"当前账户不能创建符号链接: {error}")

        with self.assertRaisesRegex(LibraryError, "链接"):
            self.service.delete_images("123")

        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(
            (self.paths.pictures / "123" / "旧章节" / "1.jpg").is_file()
        )

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_descendant_blocks_delete_without_crossing_boundary(self):
        self._write("Pictures/123/旧章节/1.jpg", b"image")
        outside = self.paths.root / "outside-directory"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"keep")
        junction = self.paths.pictures / "123" / "旧章节" / "outside-junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        try:
            with self.assertRaisesRegex(LibraryError, "链接"):
                self.service.delete_images("123")
            self.assertEqual(sentinel.read_bytes(), b"keep")
        finally:
            if junction.exists():
                os.rmdir(junction)

    def _manifest(self):
        return ChapterManifest(
            version=1,
            album_id="123",
            album_title="远端原始标题",
            album_dir_name="固定目录",
            chapters=(
                ChapterManifestEntry("301", 1, "第一章", "第1章", 1),
                ChapterManifestEntry("302", 2, "第二章", "第2章", 1),
            ),
        )

    def _write(self, relative: str, content: bytes):
        path = self.paths.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
