import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jm_downloader.library import (
    ChapterManifestStore,
    LibraryError,
    LibraryNotFound,
    LibraryService,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    LibraryLayout,
)
from jm_downloader.settings import AppPaths


class LibraryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.library = LibraryService(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scans_nested_layouts_and_ignores_flat_whole_pdfs(self):
        self._write("Pictures/10/旧章节/10.jpg", b"ten")
        self._write("Pictures/10/旧章节/2.jpg", b"two")
        self._write("PDFs/10.pdf", b"unmanaged")
        self._write("PDFs/30.pdf", b"unmanaged-only")
        self._write("PDFs/40/未知标题/第1章.pdf", b"nested")

        items = self.library.list_items()

        self.assertEqual([item.album_id for item in items], ["10", "40"])
        legacy, unverified = items
        self.assertEqual(legacy.layout, LibraryLayout.LEGACY)
        self.assertIsNone(legacy.title)
        self.assertEqual(legacy.chapter_count, 1)
        self.assertEqual(legacy.image_count, 2)
        self.assertFalse(legacy.has_pdf)
        self.assertEqual(
            legacy.preview_path.relative_to(self.paths.pictures).as_posix(),
            "10/旧章节/2.jpg",
        )
        self.assertEqual(unverified.layout, LibraryLayout.UNVERIFIED)
        self.assertIsNone(unverified.title)
        self.assertFalse(unverified.has_images)
        self.assertEqual(unverified.pdf_directory, self.paths.pdfs / "40")

    def test_delete_images_and_pdf_independently_for_managed_item(self):
        self._save_manifest()
        self._write("Pictures/123/测试漫画/第1章/1.jpg", b"image")
        self._write("PDFs/123/测试漫画/第1章.pdf", b"pdf")
        whole_pdf = self._write("PDFs/123.pdf", b"keep")

        self.library.delete_images("123")

        item = self.library.get_item("123")
        manifest = ChapterManifestStore(self.paths).load("123")
        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertFalse(item.has_images)
        self.assertTrue(item.has_pdf)
        self.assertEqual(item.chapter_count, 0)
        self.assertEqual(manifest.chapters, ())
        self.assertEqual(whole_pdf.read_bytes(), b"keep")

        self.library.delete_pdf("123")
        self.assertFalse((self.paths.pdfs / "123").exists())
        self.assertEqual(whole_pdf.read_bytes(), b"keep")
        with self.assertRaises(LibraryNotFound):
            self.library.get_item("123")

    def test_delete_all_removes_only_nested_managed_roots(self):
        self._save_manifest()
        self._write("Pictures/123/测试漫画/第1章/1.jpg", b"image")
        self._write("PDFs/123/测试漫画/第1章.pdf", b"pdf")
        whole_pdf = self._write("PDFs/123.pdf", b"keep")

        self.library.delete_all("123")

        self.assertFalse((self.paths.pictures / "123").exists())
        self.assertFalse((self.paths.pdfs / "123").exists())
        self.assertEqual(whole_pdf.read_bytes(), b"keep")
        with self.assertRaises(LibraryNotFound):
            self.library.delete_all("123")

    def test_delete_all_rolls_back_images_when_pdf_cannot_be_staged(self):
        self._save_manifest()
        image_path = self._write(
            "Pictures/123/测试漫画/第1章/1.jpg",
            b"image",
        )
        pdf_path = self._write(
            "PDFs/123/测试漫画/第1章.pdf",
            b"pdf",
        )
        pdf_root = self.paths.pdfs / "123"
        original_replace = os.replace

        def replace_with_locked_pdf(source, destination):
            if Path(source) == pdf_root:
                raise PermissionError("PDF 文件夹被占用")
            return original_replace(source, destination)

        with patch(
            "jm_downloader.library.os.replace",
            side_effect=replace_with_locked_pdf,
        ):
            with self.assertRaisesRegex(LibraryError, "删除漫画失败"):
                self.library.delete_all("123")

        self.assertEqual(image_path.read_bytes(), b"image")
        self.assertEqual(pdf_path.read_bytes(), b"pdf")
        self.assertEqual(list(self.paths.pictures.glob(".*.delete")), [])
        self.assertEqual(list(self.paths.pdfs.glob(".*.delete")), [])

    def test_rejects_invalid_album_ids(self):
        with self.assertRaises(LibraryNotFound):
            self.library.get_preview("../secret")

    def test_hidden_rebuild_api_still_wraps_legacy_builder_failure(self):
        self._write("Pictures/123/旧章节/1.jpg", b"image")
        old_pdf = self._write("PDFs/123.pdf", b"old pdf")
        with patch(
            "jm_downloader.library.album_to_pdf",
            side_effect=OSError("文件被占用"),
        ):
            with self.assertRaisesRegex(LibraryError, "PDF 生成失败"):
                self.library.rebuild_pdf("123")

        self.assertEqual(old_pdf.read_bytes(), b"old pdf")

    def test_open_pdf_uses_verified_managed_title_directory(self):
        self._save_manifest()
        self._write("Pictures/123/测试漫画/第1章/1.jpg", b"image")
        pdf_directory = self.paths.pdfs / "123" / "测试漫画"
        self._write("PDFs/123/测试漫画/第1章.pdf", b"pdf")

        with patch("jm_downloader.library.os.startfile", create=True) as startfile:
            self.library.open_location("123", "pdf")

        startfile.assert_called_once_with(pdf_directory.resolve())

    def test_open_pdf_only_item_uses_conservative_album_root(self):
        self._write("PDFs/123/外部目录/第1章.pdf", b"pdf")

        with patch("jm_downloader.library.os.startfile", create=True) as startfile:
            self.library.open_location("123", "pdf")

        startfile.assert_called_once_with((self.paths.pdfs / "123").resolve())

    def _save_manifest(self):
        manifest = ChapterManifest(
            version=1,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    "301",
                    1,
                    "第一章",
                    "第1章",
                    1,
                ),
            ),
        )
        ChapterManifestStore(self.paths).merge_and_save(manifest)
        return manifest

    def _write(self, relative_path: str, content: bytes):
        path = self.paths.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
