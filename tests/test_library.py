import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jm_downloader.library import LibraryError, LibraryNotFound, LibraryService
from jm_downloader.settings import AppPaths


class LibraryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.library = LibraryService(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scans_images_and_pdfs_from_disk(self):
        (self.paths.pictures / "5").mkdir()
        self._write("Pictures/10/2/10.jpg", b"ten")
        self._write("Pictures/10/2/2.jpg", b"two")
        self._write("Pictures/2/1/1.jpg", b"one")
        self._write("PDFs/10.pdf", b"pdf")
        self._write("PDFs/30.pdf", b"pdf only")

        items = self.library.list_items()

        self.assertEqual([item.album_id for item in items], ["2", "10", "30"])
        item = items[1]
        self.assertEqual(item.chapter_count, 1)
        self.assertEqual(item.image_count, 2)
        self.assertTrue(item.has_pdf)
        self.assertEqual(
            item.preview_path.relative_to(self.paths.pictures).as_posix(),
            "10/2/2.jpg",
        )
        self.assertEqual(item.pdf_path, self.paths.pdfs / "10.pdf")
        self.assertEqual(
            self.library.get_preview("10").relative_to(self.paths.pictures).as_posix(),
            "10/2/2.jpg",
        )

    def test_delete_images_and_pdf_independently(self):
        self._write("Pictures/123/1/1.jpg", b"image")
        self._write("PDFs/123.pdf", b"pdf")

        self.library.delete_images("123")
        item = self.library.get_item("123")
        self.assertFalse(item.has_images)
        self.assertTrue(item.has_pdf)

        self.library.delete_pdf("123")
        with self.assertRaises(LibraryNotFound):
            self.library.get_item("123")

    def test_delete_all_removes_images_and_pdf(self):
        self._write("Pictures/123/1/1.jpg", b"image")
        self._write("PDFs/123.pdf", b"pdf")

        self.library.delete_all("123")

        self.assertFalse((self.paths.pictures / "123").exists())
        self.assertFalse((self.paths.pdfs / "123.pdf").exists())
        with self.assertRaises(LibraryNotFound):
            self.library.delete_all("123")

    def test_delete_all_rolls_back_images_when_pdf_cannot_be_staged(self):
        image_path = self.paths.pictures / "123" / "1" / "1.jpg"
        pdf_path = self.paths.pdfs / "123.pdf"
        self._write("Pictures/123/1/1.jpg", b"image")
        self._write("PDFs/123.pdf", b"pdf")
        original_replace = os.replace

        def replace_with_locked_pdf(source, destination):
            if Path(source) == pdf_path:
                raise PermissionError("PDF 文件被占用")
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

    def test_rebuild_pdf_uses_managed_directories(self):
        self._write("Pictures/123/1/1.jpg", b"image")
        expected = self.paths.pdfs / "123.pdf"
        with patch("jm_downloader.library.album_to_pdf", return_value=str(expected)) as build:
            result = self.library.rebuild_pdf("123")

        self.assertEqual(result, str(expected))
        build.assert_called_once_with(
            str(self.paths.pictures / "123"), str(self.paths.pdfs)
        )

    def test_rebuild_pdf_wraps_failure_without_removing_existing_pdf(self):
        self._write("Pictures/123/1/1.jpg", b"image")
        self._write("PDFs/123.pdf", b"old pdf")
        with patch(
            "jm_downloader.library.album_to_pdf",
            side_effect=OSError("文件被占用"),
        ):
            with self.assertRaisesRegex(LibraryError, "PDF 生成失败"):
                self.library.rebuild_pdf("123")

        self.assertEqual((self.paths.pdfs / "123.pdf").read_bytes(), b"old pdf")

    def test_open_pdf_uses_system_default_application(self):
        pdf_path = self.paths.pdfs / "123.pdf"
        self._write("PDFs/123.pdf", b"pdf")

        with patch("jm_downloader.library.os.startfile", create=True) as startfile:
            self.library.open_location("123", "pdf")

        startfile.assert_called_once_with(pdf_path)

    def _write(self, relative_path: str, content: bytes):
        path = self.paths.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
