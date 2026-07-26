"""v2.9.1 Phase 5: explicit legacy layout recognition and migration."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jm_downloader.library import (
    ChapterManifestError,
    ChapterManifestStore,
    LibraryError,
    LibraryService,
)
from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    LibraryLayout,
)
from jm_downloader.settings import AppPaths


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 4), color).save(path)
    return path


def _catalog(*chapters, title="迁移漫画") -> ChapterCatalogSnapshot:
    return ChapterCatalogSnapshot(
        album_id="123",
        title=title,
        chapters=tuple(
            ChapterSnapshot(photo_id, index, chapter_title)
            for photo_id, index, chapter_title in chapters
        ),
    )


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.store = ChapterManifestStore(self.paths)

    def _legacy_image(
        self,
        directory: str | None,
        *,
        name: str = "1.jpg",
        color: str = "green",
    ) -> Path:
        root = self.paths.pictures / "123"
        if directory is not None:
            root /= directory
        return _write_image(root / name, color)

    def test_multi_chapter_exact_mapping_migrates_to_managed_layout(self):
        first = self._legacy_image("第一章")
        second = self._legacy_image("第二章", color="blue")
        flat_pdf = self.paths.pdfs / "123.pdf"
        flat_pdf.write_bytes(b"keep")
        catalog = _catalog(
            ("301", 1, "第一章"),
            ("302", 2, "第二章"),
        )

        plan = self.service.plan_legacy_migration("123", catalog)
        manifest = self.service.migrate_legacy_layout(plan)

        self.assertEqual(
            [(item.source_name, item.target_dir_name) for item in plan.mappings],
            [("第一章", "第1章"), ("第二章", "第2章")],
        )
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(
            (
                self.paths.pictures
                / "123"
                / "迁移漫画"
                / "第1章"
                / "1.jpg"
            ).is_file()
        )
        self.assertEqual(
            [chapter.photo_id for chapter in manifest.chapters],
            ["301", "302"],
        )
        self.assertTrue(
            all(chapter.package_format is None for chapter in manifest.chapters)
        )
        self.assertEqual(
            self.service.get_item("123").layout,
            LibraryLayout.MANAGED,
        )
        self.assertEqual(flat_pdf.read_bytes(), b"keep")

    def test_single_direct_images_migrate_without_extra_chapter_directory(self):
        self._legacy_image(None)
        catalog = _catalog(("301", 1, "唯一章节"))

        plan = self.service.plan_legacy_migration("123", catalog)
        self.service.migrate_legacy_layout(plan)

        self.assertTrue(plan.direct_images)
        self.assertEqual(plan.mappings[0].target_dir_name, "")
        self.assertTrue(
            (
                self.paths.pictures
                / "123"
                / "迁移漫画"
                / "1.jpg"
            ).is_file()
        )

    def test_single_legacy_directory_collapses_to_direct_managed_images(self):
        self._legacy_image("唯一章节")
        catalog = _catalog(("301", 1, "唯一章节"))

        plan = self.service.plan_legacy_migration("123", catalog)
        self.service.migrate_legacy_layout(plan)

        self.assertFalse(plan.direct_images)
        self.assertEqual(plan.mappings[0].target_dir_name, "")
        self.assertTrue(
            (
                self.paths.pictures
                / "123"
                / "迁移漫画"
                / "1.jpg"
            ).is_file()
        )

    def test_historical_windows_name_normalization_matches_exactly(self):
        self._legacy_image("A_B_ C")
        catalog = _catalog(("301", 1, "A:B? C"))

        plan = self.service.plan_legacy_migration("123", catalog)

        self.assertEqual(plan.mappings[0].photo_id, "301")

    def test_ambiguous_remote_names_leave_disk_unchanged(self):
        image = self._legacy_image("同名")
        catalog = _catalog(
            ("301", 1, "同名"),
            ("302", 2, "同名"),
        )

        with self.assertRaisesRegex(LibraryError, "无法唯一对应"):
            self.service.plan_legacy_migration("123", catalog)

        self.assertTrue(image.is_file())
        self.assertIsNone(self.store.load("123"))

    def test_unmatched_or_mixed_legacy_content_leaves_disk_unchanged(self):
        image = self._legacy_image("本地名称")
        extra = image.parent / "notes.txt"
        extra.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(LibraryError, "非图片"):
            self.service.plan_legacy_migration(
                "123",
                _catalog(("301", 1, "远端名称")),
            )

        self.assertTrue(image.is_file())
        self.assertTrue(extra.is_file())

    def test_stale_preview_is_rejected_before_staging(self):
        image = self._legacy_image("第一章")
        plan = self.service.plan_legacy_migration(
            "123",
            _catalog(("301", 1, "第一章")),
        )
        _write_image(image.parent / "2.jpg", "blue")

        with self.assertRaisesRegex(LibraryError, "发生变化"):
            self.service.migrate_legacy_layout(plan)

        self.assertTrue(image.is_file())
        self.assertIsNone(self.store.load("123"))

    def test_manifest_publish_failure_restores_complete_legacy_layout(self):
        image = self._legacy_image("第一章")
        plan = self.service.plan_legacy_migration(
            "123",
            _catalog(("301", 1, "第一章")),
        )

        with patch.object(
            ChapterManifestStore,
            "replace_exact",
            side_effect=ChapterManifestError("locked"),
        ):
            with self.assertRaisesRegex(LibraryError, "迁移失败"):
                self.service.migrate_legacy_layout(plan)

        self.assertTrue(image.is_file())
        self.assertIsNone(self.store.load("123"))
        self.assertEqual(
            list(self.paths.pictures.glob(".123.*.migrate")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
