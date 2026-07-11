import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jm_downloader import downloader
from jm_downloader.settings import AppPaths


class DownloadWorkerTests(unittest.TestCase):
    def test_run_reports_info_progress_and_completion(self):
        events = []

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            option = Mock()
            option.dir_rule = Mock()

            paths = AppPaths(project_root)
            worker = downloader.DownloadWorker(
                "123456",
                on_info=lambda *args: events.append(("info", args)),
                on_progress=lambda *args: events.append(("progress", args)),
                on_complete=lambda *args: events.append(("complete", args)),
                on_error=lambda *args: events.append(("error", args)),
                on_preview=lambda *args: events.append(("preview", args)),
                paths=paths,
            )
            worker.fetch_info = Mock(return_value=("测试漫画", "https://example.test/cover.jpg", 2))
            worker._make_option = Mock(return_value=option)

            def fake_download(album_id, received_option, downloader):
                self.assertEqual(album_id, "123456")
                self.assertIs(received_option, option)
                active_downloader = downloader(received_option)
                album = Mock()
                photo = Mock(from_album=album, title="第一章")
                first_image = Mock(from_photo=photo)
                second_image = Mock(from_photo=photo)
                active_downloader.download_success_dict = {album: {photo: []}}

                chapter_dir = project_root / "Pictures" / "123456" / "第一章"
                self._write_image(chapter_dir / "2.jpg")
                active_downloader.after_image(second_image, str(chapter_dir / "2.jpg"))
                self._write_image(chapter_dir / "1.jpg")
                active_downloader.after_image(first_image, str(chapter_dir / "1.jpg"))

            expected_pdf = project_root / "PDFs" / "123456.pdf"
            with (
                patch.object(downloader.jmcomic, "download_album", side_effect=fake_download),
                patch.object(downloader, "album_to_pdf", return_value=str(expected_pdf)) as make_pdf,
            ):
                worker.run()

            self.assertEqual(option.dir_rule.base_dir, str(project_root / "Pictures"))
            make_pdf.assert_called_once_with(
                str(project_root / "Pictures" / "123456"),
                str(project_root / "PDFs"),
            )
            self.assertEqual(events[0], ("info", ("123456", "测试漫画", "https://example.test/cover.jpg")))
            progress_events = [event for event in events if event[0] == "progress"]
            self.assertEqual(progress_events[0], ("progress", ("123456", 47, "第一章", "1/2")))
            self.assertEqual(progress_events[1], ("progress", ("123456", 94, "第一章", "2/2")))
            self.assertEqual(progress_events[2], ("progress", ("123456", 95, "打包 PDF", "")))
            preview_events = [event for event in events if event[0] == "preview"]
            self.assertEqual(
                [Path(event[1][1]).name for event in preview_events],
                ["2.jpg", "1.jpg"],
            )
            self.assertEqual(events[-1], ("complete", ("123456", str(expected_pdf))))

    def test_run_reports_download_errors(self):
        errors = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                on_error=lambda album_id, message: errors.append((album_id, message)),
                paths=AppPaths(Path(temp_dir)),
            )
            worker.fetch_info = Mock(side_effect=RuntimeError("network failed"))

            worker.run()

        self.assertEqual(errors, [("123456", "network failed")])

    @staticmethod
    def _write_image(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")


if __name__ == "__main__":
    unittest.main()
