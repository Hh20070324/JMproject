import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jm_downloader.pdf import (
    PdfPublishAborted,
    PdfSourcePathError,
    chapter_to_pdf,
)


class ChapterToPdfTests(unittest.TestCase):
    def test_uses_direct_images_in_natural_order_and_exact_output_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter_dir = root / "Pictures" / "123" / "漫画名" / "第2章"
            output = root / "PDFs" / "123" / "漫画名" / "caller-name.pdf"
            self._write_image(chapter_dir / "10.jpg", (0, 0, 255))
            self._write_image(chapter_dir / "2.jpg", (255, 0, 0))
            self._write_image(chapter_dir / "nested" / "1.jpg", (0, 255, 0))

            opened_paths = []
            original_open = Image.open

            def record_open(path, *args, **kwargs):
                opened_paths.append(Path(path).relative_to(chapter_dir).as_posix())
                return original_open(path, *args, **kwargs)

            with patch("jm_downloader.pdf.Image.open", side_effect=record_open):
                result = chapter_to_pdf(chapter_dir, output)

            self.assertEqual(result, str(output))
            self.assertEqual(opened_paths, ["2.jpg", "10.jpg"])
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes()[:4], b"%PDF")
            self.assertFalse((output.parent / "第2章.pdf").exists())
            self.assertEqual(list(output.parent.glob("*.pdf.part")), [])

    def test_replaces_through_same_directory_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter_dir = root / "chapter"
            output = root / "nested" / "chosen.pdf"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"old")
            self._write_image(chapter_dir / "1.jpg", (1, 2, 3))
            original_replace = os.replace
            replacements = []

            def record_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                return original_replace(source, destination)

            with patch("jm_downloader.pdf.os.replace", side_effect=record_replace):
                result = chapter_to_pdf(chapter_dir, output)

            self.assertEqual(result, str(output))
            self.assertEqual(len(replacements), 1)
            source, destination = replacements[0]
            self.assertEqual(source.parent, output.parent)
            self.assertEqual(destination, output)
            self.assertNotEqual(output.read_bytes(), b"old")
            self.assertEqual(list(output.parent.glob("*.pdf.part")), [])

    def test_publish_guard_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter_dir = root / "chapter"
            output = root / "pdfs" / "chapter.pdf"
            output.parent.mkdir()
            output.write_bytes(b"old")
            self._write_image(chapter_dir / "1.jpg", (1, 2, 3))

            with self.assertRaises(PdfPublishAborted):
                chapter_to_pdf(
                    chapter_dir,
                    output,
                    lambda: False,
                )

            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(output.parent.glob("*.pdf.part")), [])

    def test_rejects_linked_image_instead_of_silently_omitting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter_dir = root / "chapter"
            output = root / "pdfs" / "chapter.pdf"
            self._write_image(chapter_dir / "1.jpg", (1, 2, 3))
            outside = root / "outside.jpg"
            self._write_image(outside, (4, 5, 6))
            link = chapter_dir / "2.jpg"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("当前环境不允许创建符号链接")

            with self.assertRaises(PdfSourcePathError):
                chapter_to_pdf(chapter_dir, output)

            self.assertFalse(output.exists())

    def test_rejects_chapter_directory_reached_through_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual = root / "actual"
            self._write_image(actual / "1.jpg", (1, 2, 3))
            linked_parent = root / "linked"
            try:
                linked_parent.symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("当前环境不允许创建符号链接")

            with self.assertRaises(PdfSourcePathError):
                chapter_to_pdf(linked_parent, root / "pdfs" / "chapter.pdf")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_rejects_chapter_directory_junction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual = root / "actual"
            self._write_image(actual / "1.jpg", (1, 2, 3))
            junction = root / "chapter-junction"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(actual)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest("当前环境不允许创建目录联接")

            try:
                with self.assertRaises(PdfSourcePathError):
                    chapter_to_pdf(junction, root / "pdfs" / "chapter.pdf")
            finally:
                os.rmdir(junction)

    @staticmethod
    def _write_image(path: Path, color: tuple[int, int, int]):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color).save(path)


if __name__ == "__main__":
    unittest.main()
