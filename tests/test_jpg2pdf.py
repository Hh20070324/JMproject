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

    @staticmethod
    def _write_image(path: Path, color: tuple[int, int, int]):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color).save(path)


if __name__ == "__main__":
    unittest.main()
