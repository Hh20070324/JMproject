"""v2.9.1 Phase 2: rebuild managed chapters in their original formats."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestError,
    ChapterManifestStore,
    LibraryService,
)
from jm_downloader.models import ChapterManifest, ChapterManifestEntry
from jm_downloader.packaging import cbz_file_intact
from jm_downloader.pdf import pdf_file_readable
from jm_downloader.settings import AppPaths


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 4), color).save(path)
    return path


def _entry(
    photo_id: str,
    index: int,
    *,
    package_format: str | None,
    dir_name: str | None = None,
) -> ChapterManifestEntry:
    return ChapterManifestEntry(
        photo_id=photo_id,
        index=index,
        title=f"第 {index} 章",
        dir_name=f"第{index}章" if dir_name is None else dir_name,
        page_count=1,
        image_format="jpg",
        downloaded_at_utc="2026-07-26T00:00:00Z",
        package_format=package_format,
    )


class ChapterRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.store = ChapterManifestStore(self.paths)

    def _save(self, chapters) -> Path:
        manifest = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=tuple(chapters),
        )
        self.store.merge_and_save(manifest)
        return self.paths.pictures / "123" / ".jm-chapters.json"

    def _image(self, dir_name: str, color: str = "green") -> Path:
        return _write_image(
            self.paths.pictures
            / "123"
            / "测试漫画"
            / dir_name
            / "1.jpg",
            color,
        )

    def _package(self, name: str) -> Path:
        return self.paths.pdfs / "123" / "测试漫画" / name

    def test_mixed_batch_uses_each_recorded_format_and_ignores_overrides(self):
        self._image("第1章")
        self._image("第2章", "blue")
        self._save(
            [
                _entry("301", 1, package_format="pdf"),
                _entry("302", 2, package_format="cbz"),
            ]
        )

        result = self.service.rebuild_chapters(
            "123",
            ("301", "302"),
            confirmed_formats={"301": "cbz", "302": "pdf"},
        )

        self.assertEqual(result.failures, ())
        self.assertEqual(
            [item.package_format for item in result.succeeded],
            ["pdf", "cbz"],
        )
        self.assertTrue(pdf_file_readable(self._package("第1章.pdf")))
        self.assertTrue(cbz_file_intact(self._package("第2章.cbz"), 1))
        self.assertFalse(self._package("第1章.cbz").exists())
        self.assertFalse(self._package("第2章.pdf").exists())

    def test_images_chapter_succeeds_without_creating_package(self):
        self._image("第1章")
        self._save([_entry("301", 1, package_format="images")])

        result = self.service.rebuild_chapters("123", ("301",))

        self.assertEqual(result.failures, ())
        self.assertEqual(result.succeeded[0].package_format, "images")
        self.assertIsNone(result.succeeded[0].output_path)
        self.assertFalse((self.paths.pdfs / "123").exists())

    def test_unknown_format_requires_confirmation_without_disk_changes(self):
        self._image("第1章")
        manifest_path = self._save(
            [_entry("301", 1, package_format=None)]
        )
        before = manifest_path.read_bytes()

        result = self.service.rebuild_chapters("123", ("301",))

        self.assertEqual(result.succeeded, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("先确认", result.failures[0].message)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertFalse((self.paths.pdfs / "123").exists())

    def test_confirmed_unknown_pdf_is_built_and_persisted(self):
        self._image("第1章")
        self._save([_entry("301", 1, package_format=None)])

        result = self.service.rebuild_chapters(
            "123",
            ("301",),
            confirmed_formats={"301": "pdf"},
        )

        self.assertEqual(result.failures, ())
        self.assertTrue(pdf_file_readable(self._package("第1章.pdf")))
        self.assertEqual(
            self.store.load("123").chapters[0].package_format,
            "pdf",
        )

    def test_confirmed_unknown_images_only_updates_manifest(self):
        self._image("第1章")
        self._save([_entry("301", 1, package_format=None)])

        result = self.service.rebuild_chapters(
            "123",
            ("301",),
            confirmed_formats={"301": "images"},
        )

        self.assertEqual(result.failures, ())
        self.assertIsNone(result.succeeded[0].output_path)
        self.assertEqual(
            self.store.load("123").chapters[0].package_format,
            "images",
        )
        self.assertFalse((self.paths.pdfs / "123").exists())

    def test_one_bad_chapter_does_not_block_later_success(self):
        self._image("第2章")
        self._save(
            [
                _entry("301", 1, package_format="pdf"),
                _entry("302", 2, package_format="cbz"),
            ]
        )

        result = self.service.rebuild_chapters(
            "123",
            ("301", "missing", "302"),
        )

        self.assertEqual(
            [failure.photo_id for failure in result.failures],
            ["301", "missing"],
        )
        self.assertEqual(
            [item.photo_id for item in result.succeeded],
            ["302"],
        )
        self.assertTrue(cbz_file_intact(self._package("第2章.cbz"), 1))

    def test_manifest_write_failure_restores_old_package_and_unknown_format(self):
        self._image("第1章")
        manifest_path = self._save(
            [_entry("301", 1, package_format=None)]
        )
        target = self._package("第1章.pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old package")
        before_manifest = manifest_path.read_bytes()

        with patch.object(
            ChapterManifestStore,
            "replace_exact",
            side_effect=ChapterManifestError("write failed"),
        ):
            result = self.service.rebuild_chapters(
                "123",
                ("301",),
                confirmed_formats={"301": "pdf"},
            )

        self.assertEqual(result.succeeded, ())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(target.read_bytes(), b"old package")
        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual(
            list(target.parent.glob(".*.rebuild")),
            [],
        )

    def test_builder_failure_restores_existing_package(self):
        self._image("第1章")
        self._save([_entry("301", 1, package_format="pdf")])
        target = self._package("第1章.pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old package")

        with patch(
            "jm_downloader.library.chapter_to_pdf",
            side_effect=OSError("locked"),
        ):
            result = self.service.rebuild_chapters("123", ("301",))

        self.assertEqual(result.succeeded, ())
        self.assertEqual(target.read_bytes(), b"old package")
        self.assertEqual(
            list(target.parent.glob(".*.rebuild")),
            [],
        )

    def test_single_chapter_uses_album_title_filename(self):
        _write_image(
            self.paths.pictures / "123" / "测试漫画" / "1.jpg"
        )
        self._save(
            [_entry("301", 1, package_format="pdf", dir_name="")]
        )

        result = self.service.rebuild_chapters("123", "301")

        self.assertEqual(result.failures, ())
        self.assertEqual(
            result.succeeded[0].output_path,
            self._package("测试漫画.pdf"),
        )
        self.assertTrue(pdf_file_readable(self._package("测试漫画.pdf")))

    def test_whole_album_rebuild_production_api_is_removed(self):
        self.assertFalse(hasattr(LibraryService, "rebuild_pdf"))

    def test_manifest_json_is_v3_after_confirmed_unknown_choice(self):
        self._image("第1章")
        path = self._save([_entry("301", 1, package_format=None)])

        self.service.rebuild_chapters(
            "123",
            ("301",),
            confirmed_formats={"301": "cbz"},
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["chapters"][0]["package_format"], "cbz")


if __name__ == "__main__":
    unittest.main()
