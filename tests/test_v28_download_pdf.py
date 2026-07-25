import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jm_downloader import downloader
from jm_downloader.models import ChapterManifest, ChapterManifestEntry
from jm_downloader.settings import AppPaths


class V28DownloadPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_multi_chapter_download_builds_one_pdf_per_chapter(self):
        worker = self._worker_with_manifest(
            "123",
            "多章漫画",
            (
                ChapterManifestEntry("301", 1, "第一章", "第1章", 1),
                ChapterManifestEntry("302", 2, "第二章", "第2章", 1),
            ),
        )
        self._write_image(
            self.paths.pictures / "123" / "多章漫画" / "第1章" / "1.jpg"
        )
        self._write_image(
            self.paths.pictures / "123" / "多章漫画" / "第2章" / "1.jpg"
        )

        result = worker._package_chapter_pdfs()

        expected = self.paths.pdfs / "123" / "多章漫画"
        self.assertEqual(result, expected.resolve())
        self.assertTrue((expected / "第1章.pdf").is_file())
        self.assertTrue((expected / "第2章.pdf").is_file())
        self.assertFalse((self.paths.pdfs / "123.pdf").exists())

    def test_actual_single_chapter_uses_album_title_for_pdf_name(self):
        worker = self._worker_with_manifest(
            "456",
            "单章漫画",
            (ChapterManifestEntry("401", 1, "单章", "", 1),),
        )
        self._write_image(
            self.paths.pictures / "456" / "单章漫画" / "1.jpg"
        )

        result = worker._package_chapter_pdfs()

        self.assertEqual(
            result,
            (self.paths.pdfs / "456" / "单章漫画").resolve(),
        )
        self.assertTrue((result / "单章漫画.pdf").is_file())
        self.assertFalse((result / "第1章.pdf").exists())

    def test_later_chapter_failure_keeps_earlier_published_pdf(self):
        worker = self._worker_with_manifest(
            "789",
            "中途失败",
            (
                ChapterManifestEntry("501", 1, "第一章", "第1章", 1),
                ChapterManifestEntry("502", 2, "第二章", "第2章", 1),
            ),
        )
        for chapter_name in ("第1章", "第2章"):
            self._write_image(
                self.paths.pictures
                / "789"
                / "中途失败"
                / chapter_name
                / "1.jpg"
            )
        original = downloader.chapter_to_pdf
        calls = 0

        def fail_second(chapter_dir, out_path, publish_guard=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(
                chapter_dir,
                out_path,
                publish_guard=publish_guard,
            )

        with (
            patch.object(
                downloader,
                "chapter_to_pdf",
                side_effect=fail_second,
            ),
            self.assertRaises(downloader.PdfPackagingError),
        ):
            worker._package_chapter_pdfs()

        output = self.paths.pdfs / "789" / "中途失败"
        self.assertTrue((output / "第1章.pdf").is_file())
        self.assertFalse((output / "第2章.pdf").exists())
        self.assertIsNone(worker._manifest_store.load("789"))

    def test_cancelled_packaging_keeps_first_pdf_and_retry_converges(self):
        chapters = (
            ChapterManifestEntry("701", 1, "第一章", "第1章", 1),
            ChapterManifestEntry("702", 2, "第二章", "第2章", 1),
        )
        worker = self._worker_with_manifest(
            "700",
            "取消后继续",
            chapters,
        )
        for chapter_name in ("第1章", "第2章"):
            self._write_image(
                self.paths.pictures
                / "700"
                / "取消后继续"
                / chapter_name
                / "1.jpg"
            )
        original = downloader.chapter_to_pdf

        def stop_after_first(chapter_dir, out_path, publish_guard=None):
            result = original(
                chapter_dir,
                out_path,
                publish_guard=publish_guard,
            )
            worker.stop()
            return result

        with (
            patch.object(
                downloader,
                "chapter_to_pdf",
                side_effect=stop_after_first,
            ),
            self.assertRaises(downloader.DownloadStopped),
        ):
            worker._package_chapter_pdfs()

        output = self.paths.pdfs / "700" / "取消后继续"
        self.assertTrue((output / "第1章.pdf").is_file())
        self.assertFalse((output / "第2章.pdf").exists())
        self.assertEqual(tuple(output.glob("*.part")), ())
        self.assertIsNone(worker._manifest_store.load("700"))

        retry = self._worker_with_manifest(
            "700",
            "取消后继续",
            chapters,
        )
        result = retry._package_chapter_pdfs()

        self.assertEqual(result, output.resolve())
        self.assertTrue((output / "第1章.pdf").is_file())
        self.assertTrue((output / "第2章.pdf").is_file())
        self.assertEqual(tuple(output.glob("*.part")), ())

    def test_stale_extra_image_is_rejected_before_pdf_publish(self):
        worker = self._worker_with_manifest(
            "999",
            "残留检查",
            (ChapterManifestEntry("601", 1, "第一章", "第1章", 1),),
        )
        chapter_directory = (
            self.paths.pictures / "999" / "残留检查" / "第1章"
        )
        self._write_image(chapter_directory / "1.jpg")
        self._write_image(chapter_directory / "2.jpg")

        with self.assertRaises(downloader.DownloadIntegrityError):
            worker._package_chapter_pdfs()

        self.assertEqual(
            tuple((self.paths.pdfs / "999").rglob("*.pdf")),
            (),
        )

    def _worker_with_manifest(
        self,
        album_id: str,
        album_dir_name: str,
        chapters: tuple[ChapterManifestEntry, ...],
    ) -> downloader.DownloadWorker:
        worker = downloader.DownloadWorker(
            album_id,
            paths=self.paths,
            selected_chapter_ids=tuple(
                chapter.photo_id for chapter in chapters
            ),
        )
        worker._pending_manifest = ChapterManifest(
            version=1,
            album_id=album_id,
            album_title=album_dir_name,
            album_dir_name=album_dir_name,
            chapters=chapters,
        )
        return worker

    @staticmethod
    def _write_image(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), "white").save(path, "JPEG")


if __name__ == "__main__":
    unittest.main()
