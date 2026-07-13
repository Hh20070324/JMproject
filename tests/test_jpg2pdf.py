import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jm_downloader.pdf import album_to_pdf, natural_key


class NaturalKeyTests(unittest.TestCase):
    def test_sorts_numeric_names_naturally(self):
        names = ["10.jpg", "2.jpg", "1.jpg", "page11.jpg", "page3.jpg"]

        self.assertEqual(
            sorted(names, key=natural_key),
            ["1.jpg", "2.jpg", "10.jpg", "page3.jpg", "page11.jpg"],
        )


class AlbumToPdfTests(unittest.TestCase):
    def test_merges_chapters_and_pages_in_natural_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album_dir = root / "123456"
            output_dir = root / "pdfs"

            self._write_image(album_dir / "10" / "1.jpg", (0, 0, 255))
            self._write_image(album_dir / "2" / "10.jpg", (0, 255, 0))
            self._write_image(album_dir / "2" / "2.jpg", (255, 0, 0))

            opened_paths = []
            original_open = Image.open

            def record_open(path, *args, **kwargs):
                opened_paths.append(Path(path).relative_to(album_dir).as_posix())
                return original_open(path, *args, **kwargs)

            with patch("jm_downloader.pdf.Image.open", side_effect=record_open):
                result = album_to_pdf(str(album_dir), str(output_dir))

            pdf_path = output_dir / "123456.pdf"
            self.assertEqual(result, str(pdf_path))
            self.assertTrue(pdf_path.is_file())
            self.assertEqual(pdf_path.read_bytes()[:4], b"%PDF")
            self.assertEqual(opened_paths, ["2/2.jpg", "2/10.jpg", "10/1.jpg"])
            self.assertEqual(list(output_dir.glob("*.pdf.part")), [])

    def test_failed_pdf_write_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album_dir = root / "123456"
            output_dir = root / "pdfs"
            output_dir.mkdir()
            pdf_path = output_dir / "123456.pdf"
            pdf_path.write_bytes(b"existing pdf")
            self._write_image(album_dir / "chapter" / "1.jpg", (1, 2, 3))

            with patch(
                "jm_downloader.pdf.Image.Image.save",
                side_effect=OSError("write failed"),
            ):
                with self.assertRaisesRegex(OSError, "write failed"):
                    album_to_pdf(str(album_dir), str(output_dir))

            self.assertEqual(pdf_path.read_bytes(), b"existing pdf")
            self.assertEqual(list(output_dir.glob("*.pdf.part")), [])

    def test_failed_pdf_replace_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album_dir = root / "123456"
            output_dir = root / "pdfs"
            output_dir.mkdir()
            pdf_path = output_dir / "123456.pdf"
            pdf_path.write_bytes(b"existing pdf")
            self._write_image(album_dir / "chapter" / "1.jpg", (1, 2, 3))

            with patch(
                "jm_downloader.pdf.os.replace",
                side_effect=PermissionError("PDF 文件被占用"),
            ):
                with self.assertRaisesRegex(PermissionError, "PDF 文件被占用"):
                    album_to_pdf(str(album_dir), str(output_dir))

            self.assertEqual(pdf_path.read_bytes(), b"existing pdf")
            self.assertEqual(list(output_dir.glob("*.pdf.part")), [])

    def test_includes_nested_images_but_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album_dir = root / "123456"
            output_dir = root / "pdfs"
            nested = album_dir / "chapter" / "nested" / "1.jpg"
            self._write_image(nested, (1, 2, 3))
            outside = root / "outside.jpg"
            self._write_image(outside, (4, 5, 6))
            link = album_dir / "chapter" / "2.jpg"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("当前环境不允许创建符号链接")

            opened_paths = []
            original_open = Image.open

            def record_open(path, *args, **kwargs):
                opened_paths.append(Path(path).resolve())
                return original_open(path, *args, **kwargs)

            with patch("jm_downloader.pdf.Image.open", side_effect=record_open):
                album_to_pdf(str(album_dir), str(output_dir))

            self.assertEqual(opened_paths, [nested.resolve()])

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_ignores_directory_junctions_outside_album(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            album_dir = root / "123456"
            output_dir = root / "pdfs"
            nested = album_dir / "chapter" / "1.jpg"
            self._write_image(nested, (1, 2, 3))
            outside_dir = root / "outside"
            outside_image = outside_dir / "2.jpg"
            self._write_image(outside_image, (4, 5, 6))
            junction = album_dir / "outside-link"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest("当前环境不允许创建目录联接")

            opened_paths = []
            original_open = Image.open

            def record_open(path, *args, **kwargs):
                opened_paths.append(Path(path).resolve())
                return original_open(path, *args, **kwargs)

            try:
                with patch("jm_downloader.pdf.Image.open", side_effect=record_open):
                    album_to_pdf(str(album_dir), str(output_dir))
            finally:
                os.rmdir(junction)

            self.assertEqual(opened_paths, [nested.resolve()])

    @staticmethod
    def _write_image(path: Path, color: tuple[int, int, int]):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color).save(path)


if __name__ == "__main__":
    unittest.main()
