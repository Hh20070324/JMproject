import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from jm_downloader import downloader
from jm_downloader.settings import AppPaths


class DownloadWorkerTests(unittest.TestCase):
    def test_make_option_overrides_image_concurrency_in_memory(self):
        option = Mock()
        option.download.threading.image = 30
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                paths=AppPaths(Path(temp_dir)),
                image_concurrency=7,
            )

            def install_logging():
                calls.append("install")

            def create_option_file(_option_path):
                calls.append("create")
                return option

            with (
                patch.object(
                    downloader,
                    "install_safe_jmcomic_logging",
                    side_effect=install_logging,
                ) as install,
                patch.object(
                    downloader.jmcomic,
                    "create_option_by_file",
                    side_effect=create_option_file,
                ) as create_option,
            ):
                result = worker._make_option()

        self.assertIs(result, option)
        self.assertEqual(option.download.threading.image, 7)
        self.assertEqual(calls, ["install", "create"])
        install.assert_called_once_with()
        create_option.assert_called_once_with(str(worker.paths.option_file))

    def test_fetch_info_installs_safe_logging_before_option_use(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                paths=AppPaths(Path(temp_dir)),
            )

            def make_option():
                calls.append("make")
                raise RuntimeError("stop")

            worker._make_option = Mock(side_effect=make_option)

            with patch.object(
                downloader,
                "install_safe_jmcomic_logging",
                side_effect=lambda: calls.append("install"),
            ):
                result = worker.fetch_info()

        self.assertEqual(result, (None, None, 0))
        self.assertEqual(calls, ["install", "make"])

    def test_run_installs_safe_logging_before_option_use(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                paths=AppPaths(Path(temp_dir)),
            )

            def make_option():
                calls.append("make")
                raise RuntimeError("stop")

            worker._make_option = Mock(side_effect=make_option)

            with (
                patch.object(
                    downloader,
                    "install_safe_jmcomic_logging",
                    side_effect=lambda: calls.append("install"),
                ),
                self.assertLogs("jm-downloader", logging.ERROR),
            ):
                worker.run()

        self.assertEqual(calls, ["install", "make"])

    def test_pre_stopped_worker_reports_stopped_without_building_option(self):
        stopped = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                on_stopped=stopped.append,
                paths=AppPaths(Path(temp_dir)),
            )
            worker._make_option = Mock(side_effect=AssertionError("must not run"))
            worker.stop()
            worker.run()

        self.assertEqual(stopped, ["123456"])
        worker._make_option.assert_not_called()

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
                on_stopped=lambda *args: events.append(("stopped", args)),
                paths=paths,
            )
            worker._make_option = Mock(return_value=option)

            def fake_download(album_id, received_option, downloader):
                self.assertEqual(album_id, "123456")
                self.assertIs(received_option, option)
                active_downloader = downloader(received_option)
                album = MagicMock()
                album.id = "123456"
                album.author = "作者"
                album.page_count = 2
                album.name = "测试漫画"
                album.title = None
                album.cover = "https://example.test/cover.jpg"
                album.tags = []
                album.__len__.return_value = 1
                photo = Mock(from_album=album, title="第一章")
                first_image = Mock(from_photo=photo)
                second_image = Mock(from_photo=photo)
                active_downloader.download_success_dict = {album: {photo: []}}
                active_downloader.before_album(album)

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
            self.assertEqual(events[-2], ("complete", ("123456", str(expected_pdf))))
            self.assertEqual(events[-1], ("stopped", ("123456",)))

    def test_run_reports_download_errors(self):
        errors = []
        stopped = []
        secret = "network failed with token=secret"
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = downloader.DownloadWorker(
                "123456",
                on_error=lambda album_id, message: errors.append((album_id, message)),
                on_stopped=stopped.append,
                paths=AppPaths(Path(temp_dir)),
            )
            worker._make_option = Mock(side_effect=RuntimeError(secret))

            with self.assertLogs("jm-downloader", logging.ERROR) as logs:
                worker.run()

        self.assertEqual(
            errors,
            [("123456", "任务失败，请检查网络、配置或磁盘后重试")],
        )
        self.assertEqual(stopped, ["123456"])
        output = "\n".join(logs.output)
        self.assertIn("Download failed for JM 123456 (RuntimeError)", output)
        self.assertNotIn(secret, output)

    @staticmethod
    def _write_image(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")


if __name__ == "__main__":
    unittest.main()
