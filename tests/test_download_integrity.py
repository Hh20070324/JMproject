import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from jm_downloader import downloader
from jm_downloader.pdf import PART_FILE_MARKER, find_album_images
from jm_downloader.settings import AppPaths


class FakeOption:
    def __init__(self, target: Path, *, use_cache=True):
        self.target = target
        self.use_cache = use_cache

    def decide_image_filepath(self, _image):
        return str(self.target)

    def decide_download_cache(self, _image):
        return self.use_cache

    def decide_download_image_decode(self, _image):
        return True


class FakeClient:
    def __init__(self, writer):
        self.writer = writer
        self.calls = []

    def download_by_image_detail(self, image, target, decode_image):
        self.calls.append((image, Path(target), decode_image))
        self.writer(Path(target))


class FakeDownloader:
    def __init__(self, option, client):
        self.option = option
        self.client = client
        self.before = []
        self.after = []

    def before_image(self, image, path):
        self.before.append((image, Path(path)))

    def after_image(self, image, path):
        self.after.append((image, Path(path)))


class DownloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.worker = downloader.DownloadWorker("123", paths=self.paths)
        self.target = self.paths.pictures / "123" / "chapter" / "1.jpg"
        self.photo = SimpleNamespace(title="chapter", name="chapter")
        self.image = SimpleNamespace(
            from_photo=self.photo,
            skip=False,
            tag="1",
            img_url="https://example.test/1.jpg",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_cache_is_reused_without_network(self):
        self._write_image(self.target)
        client = FakeClient(lambda _path: self.fail("network must not run"))
        active = FakeDownloader(FakeOption(self.target), client)

        self.worker._download_image(active, self.image)

        self.assertEqual(client.calls, [])
        self.assertEqual(active.after[0][1], self.target.resolve())
        self.assertEqual(
            self.worker._expected_images,
            {self.target.resolve()},
        )
        self.assertEqual(
            self.worker._verified_images,
            {self.target.resolve()},
        )

    def test_corrupt_cache_is_replaced_with_verified_image(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"truncated")
        client = FakeClient(self._write_image)
        active = FakeDownloader(FakeOption(self.target), client)

        self.worker._download_image(active, self.image)

        self.assertTrue(self.worker._is_valid_image(self.target))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self._part_files(), [])

    def test_extension_format_mismatch_is_replaced(self):
        self.target.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), "white").save(self.target, "PNG")
        client = FakeClient(self._write_image)
        active = FakeDownloader(FakeOption(self.target), client)

        self.worker._download_image(active, self.image)

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(self.worker._is_valid_image(self.target))

    def test_invalid_download_is_not_published_and_part_is_cleaned(self):
        client = FakeClient(lambda path: path.write_bytes(b"broken"))
        active = FakeDownloader(FakeOption(self.target), client)

        with self.assertRaises(downloader.ImageValidationError):
            self.worker._download_image(active, self.image)

        self.assertFalse(self.target.exists())
        self.assertEqual(self._part_files(), [])

    def test_atomic_replace_failure_preserves_previous_valid_image(self):
        self._write_image(self.target, color="red")
        old_bytes = self.target.read_bytes()
        client = FakeClient(
            lambda path: self._write_image(path, color="blue")
        )
        active = FakeDownloader(
            FakeOption(self.target, use_cache=False),
            client,
        )

        with (
            patch.object(
                downloader.os,
                "replace",
                side_effect=OSError("replace failed"),
            ),
            self.assertRaises(OSError),
        ):
            self.worker._download_image(active, self.image)

        self.assertEqual(self.target.read_bytes(), old_bytes)
        self.assertEqual(self._part_files(), [])

    def test_stop_before_request_does_not_start_network_or_publish(self):
        client = FakeClient(lambda _path: self.fail("network must not run"))
        active = FakeDownloader(FakeOption(self.target), client)
        self.worker.stop()

        with self.assertRaises(downloader.DownloadStopped):
            self.worker._download_image(active, self.image)

        self.assertEqual(client.calls, [])
        self.assertFalse(self.target.exists())

    def test_inflight_image_finishes_atomic_publish_after_stop_request(self):
        started = threading.Event()
        release = threading.Event()

        def delayed_write(path):
            started.set()
            release.wait(timeout=2)
            self._write_image(path)

        client = FakeClient(delayed_write)
        active = FakeDownloader(FakeOption(self.target), client)
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_download_error(active, errors)
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))

        self.worker.stop()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.worker._is_valid_image(self.target))
        self.assertEqual(self._part_files(), [])

    def test_stale_parts_are_removed_and_never_enter_pdf_image_list(self):
        valid = self.target
        part = valid.with_name(
            f".{valid.stem}{PART_FILE_MARKER}old{valid.suffix}"
        )
        self._write_image(valid)
        self._write_image(part)

        self.assertEqual(find_album_images(self.paths.pictures / "123"), [valid.resolve()])
        self.worker._cleanup_stale_parts(self.paths.pictures / "123")
        self.assertFalse(part.exists())
        self.assertTrue(valid.exists())

    def test_integrity_gate_rejects_missing_verified_image(self):
        self.worker._active_downloader = SimpleNamespace(
            download_failed_image=[],
            download_failed_photo=[],
        )
        self.worker._expected_images.add(self.target.resolve())

        with self.assertRaises(downloader.DownloadIntegrityError):
            self.worker._verify_download_result()

    def _part_files(self):
        return [
            path
            for path in self.paths.pictures.rglob("*")
            if path.is_file() and PART_FILE_MARKER in path.name
        ]

    def _capture_download_error(self, active, errors):
        try:
            self.worker._download_image(active, self.image)
        except Exception as error:
            errors.append(error)

    @staticmethod
    def _write_image(path: Path, color="white"):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color).save(path, "JPEG")


if __name__ == "__main__":
    unittest.main()
