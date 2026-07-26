import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from PIL import Image

from jm_downloader.downloader import DownloadWorker
from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestStore,
    LibraryService,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    TaskConfig,
)
from jm_downloader.packaging import chapter_to_cbz
from jm_downloader.settings import AppPaths


class V29PackagingTests(unittest.TestCase):
    def test_cbz_is_naturally_ordered_and_atomically_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter = root / "chapter"
            chapter.mkdir()
            for name, color in (("10.jpg", "red"), ("2.jpg", "blue")):
                Image.new("RGB", (2, 2), color).save(chapter / name)
            output = root / "packages" / "chapter.cbz"
            output.parent.mkdir()
            output.write_bytes(b"old")

            result = chapter_to_cbz(chapter, output)

            self.assertEqual(result, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["2.jpg", "10.jpg"])
                self.assertTrue(
                    all(
                        entry.compress_type == zipfile.ZIP_STORED
                        for entry in archive.infolist()
                    )
                )
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_worker_option_uses_task_bound_image_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            option = Mock()
            option.download.threading.image = 1
            option.download.threading.photo = 1
            option.client.postman.meta_data.timeout = 1
            with patch(
                "jm_downloader.downloader.jmcomic.create_option_by_file",
                return_value=option,
            ):
                worker = DownloadWorker(
                    "123",
                    paths=paths,
                    selected_chapter_ids=("301",),
                    task_config=TaskConfig(image_format="png"),
                )
                worker._make_option()

        self.assertEqual(option.download.image.suffix, ".png")

    def test_worker_routes_pdf_cbz_and_images_to_distinct_finish_paths(self):
        for package_format in ("pdf", "cbz", "images"):
            with self.subTest(package_format=package_format):
                with tempfile.TemporaryDirectory() as temp_dir:
                    paths = AppPaths(Path(temp_dir))
                    completed = []
                    worker = DownloadWorker(
                        "123",
                        paths=paths,
                        selected_chapter_ids=("301",),
                        task_config=TaskConfig(
                            download_engine="sync",
                            package_format=package_format,
                        ),
                        on_complete=lambda album_id, directory: (
                            completed.append((album_id, directory))
                        ),
                    )
                    worker._pending_manifest = self._manifest()
                    package_dir = paths.pdfs / "123" / "漫画"
                    with (
                        patch.object(worker, "_make_option", return_value=Mock()),
                        patch(
                            "jm_downloader.downloader.jmcomic.download_album"
                        ),
                        patch.object(worker, "_verify_download_result"),
                        patch.object(
                            worker._manifest_store,
                            "merge_and_save",
                        ),
                        patch.object(
                            worker,
                            "_package_chapter_pdfs",
                            return_value=package_dir,
                        ) as pdf_packager,
                        patch.object(
                            worker,
                            "_package_chapter_cbz",
                            return_value=package_dir,
                        ) as cbz_packager,
                    ):
                        worker.run()

                self.assertEqual(
                    pdf_packager.called,
                    package_format == "pdf",
                )
                self.assertEqual(
                    cbz_packager.called,
                    package_format == "cbz",
                )
                self.assertEqual(
                    completed,
                    [
                        (
                            "123",
                            (
                                None
                                if package_format == "images"
                                else str(package_dir)
                            ),
                        )
                    ],
                )

    def test_forced_chapter_replacement_rolls_back_or_commits_as_one_unit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            worker = DownloadWorker(
                "123",
                paths=paths,
                selected_chapter_ids=("301",),
                force_redownload_chapter_ids=("301",),
                task_config=TaskConfig(image_format="png"),
            )
            worker._pending_manifest = self._manifest()
            chapter = (
                paths.pictures / "123" / "漫画" / "第1章"
            )
            chapter.mkdir(parents=True)
            (chapter / "old.jpg").write_bytes(b"old")

            worker._stage_forced_chapters()
            chapter.mkdir(parents=True)
            (chapter / "new.png").write_bytes(b"new")
            worker._rollback_replacements()

            self.assertTrue((chapter / "old.jpg").is_file())
            self.assertFalse((chapter / "new.png").exists())

            worker._stage_forced_chapters()
            chapter.mkdir(parents=True)
            (chapter / "new.png").write_bytes(b"new")
            worker._commit_replacements()

            self.assertFalse((chapter / "old.jpg").exists())
            self.assertTrue((chapter / "new.png").is_file())
            self.assertEqual(
                list((paths.pictures / "123").glob(".jm-replace-*")),
                [],
            )

    def test_v1_manifest_loads_as_v2_and_new_fields_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            manifest_path = paths.pictures / "123" / ".jm-chapters.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "album_id": "123",
                        "album_title": "旧漫画",
                        "album_dir_name": "旧漫画",
                        "chapters": [
                            {
                                "photo_id": "301",
                                "index": 1,
                                "title": "第一章",
                                "dir_name": "第1章",
                                "page_count": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = ChapterManifestStore(paths)

            loaded = store.load("123")
            published = store.merge_and_save(loaded)

            self.assertEqual(
                published.version,
                CHAPTER_MANIFEST_SCHEMA_VERSION,
            )
            self.assertIsNone(published.chapters[0].image_format)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn("downloaded_at_utc", payload["chapters"][0])

    def test_library_reports_and_deletes_pdf_and_cbz_together(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            chapter = paths.pictures / "123" / "漫画" / "第1章"
            chapter.mkdir(parents=True)
            Image.new("RGB", (2, 2), "green").save(chapter / "1.jpg")
            store = ChapterManifestStore(paths)
            store.merge_and_save(self._manifest())
            package_dir = paths.pdfs / "123" / "漫画"
            package_dir.mkdir(parents=True)
            (package_dir / "第1章.pdf").write_bytes(b"pdf")
            (package_dir / "第1章.cbz").write_bytes(b"cbz")
            service = LibraryService(paths)

            item = service.get_item("123")

            self.assertTrue(item.has_pdf)
            self.assertTrue(item.has_cbz)
            service.delete_packaged_artifacts("123")
            self.assertFalse(package_dir.exists())
            self.assertTrue(chapter.is_dir())

    @staticmethod
    def _manifest():
        return ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="漫画",
            album_dir_name="漫画",
            chapters=(
                ChapterManifestEntry(
                    "301",
                    1,
                    "第一章",
                    "第1章",
                    1,
                    image_format="jpg",
                    downloaded_at_utc="2026-07-26T00:00:00Z",
                ),
            ),
        )
