import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import download_worker


class DownloadWorkerTests(unittest.TestCase):
    def test_run_reports_info_progress_and_completion(self):
        events = []

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            option = Mock()
            option.dir_rule = Mock()

            worker = download_worker.DownloadWorker(
                "123456",
                on_info=lambda *args: events.append(("info", args)),
                on_progress=lambda *args: events.append(("progress", args)),
                on_complete=lambda *args: events.append(("complete", args)),
                on_error=lambda *args: events.append(("error", args)),
            )
            worker.fetch_info = Mock(return_value=("测试漫画", "https://example.test/cover.jpg", 2))
            worker._make_option = Mock(return_value=option)

            def fake_download(album_id, received_option, callback):
                self.assertEqual(album_id, "123456")
                self.assertIs(received_option, option)
                callback(Mock(from_album=Mock(title="第一章")), Mock())
                callback(Mock(from_album=Mock(title="第一章")), Mock())

            expected_pdf = project_root / "PDFs" / "123456.pdf"
            with (
                patch.object(download_worker, "PROJECT_ROOT", project_root),
                patch.object(download_worker.jmcomic, "download_album", side_effect=fake_download),
                patch.object(download_worker, "album_to_pdf", return_value=str(expected_pdf)) as make_pdf,
            ):
                worker.pictures_dir = project_root / "Pictures"
                worker.pdfs_dir = project_root / "PDFs"
                worker.run()

            self.assertEqual(option.dir_rule.base_dir, str(project_root / "Pictures"))
            make_pdf.assert_called_once_with(
                str(project_root / "Pictures" / "123456"),
                str(project_root / "PDFs"),
            )
            self.assertEqual(events[0], ("info", ("123456", "测试漫画", "https://example.test/cover.jpg")))
            self.assertEqual(events[1], ("progress", ("123456", 47, "第一章", "1/2")))
            self.assertEqual(events[2], ("progress", ("123456", 94, "第一章", "2/2")))
            self.assertEqual(events[3], ("progress", ("123456", 95, "打包 PDF", "")))
            self.assertEqual(events[4], ("complete", ("123456", str(expected_pdf))))

    def test_run_reports_download_errors(self):
        errors = []
        worker = download_worker.DownloadWorker(
            "123456",
            on_error=lambda album_id, message: errors.append((album_id, message)),
        )
        worker.fetch_info = Mock(side_effect=RuntimeError("network failed"))

        worker.run()

        self.assertEqual(errors, [("123456", "network failed")])


if __name__ == "__main__":
    unittest.main()
