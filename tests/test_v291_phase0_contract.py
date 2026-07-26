"""v2.9.1 Phase 0 contract baseline and offline probes.

Freezes the v2.9.0 behaviors later phases must not regress, probes the
candidate offline PDF/CBZ readability checks, and registers the v2.9.1
tracks plus user decisions 2.6/2.7.  Every test in this file passes
against unmodified v2.9.0 production code.
"""

import json
from pathlib import Path, PurePosixPath
import tempfile
import time
import unittest
import zipfile

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestError,
    ChapterManifestStore,
    CorruptChapterManifest,
    LibraryNotFound,
    LibraryService,
    UnsupportedChapterManifestVersion,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    LibraryLayout,
)
from jm_downloader.packaging import chapter_to_cbz
from jm_downloader.pdf import chapter_to_pdf
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskConflict, TaskManager


V291_TRACKS = {
    "manifest_v3": "package_format_per_chapter",
    "chapter_status": "immutable_offline_snapshot",
    "rebuild": "original_format_only",
    "chapter_delete": "images_package_all_with_rollback",
    "selective_repair": "user_checked_rebuild_or_redownload",
    "legacy_migration": "explicit_unique_mapping_only",
}

V291_USER_DECISIONS = {
    # §2.6: album-level image deletion keeps every manifest chapter
    # entry; chapters then report missing images and stay repairable.
    "album_delete_images": "preserve_chapter_entries",
    # §2.7: only the user-triggerable whole-book rebuild chain is
    # removed; album_to_pdf, backend smoke and low-level tests stay.
    "whole_pdf_removal": "user_entrypoint_only",
}


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color).save(path)
    return path


def _manifest_payload(
    album_id: str = "123",
    *,
    version: int = CHAPTER_MANIFEST_SCHEMA_VERSION,
    chapter_fields: dict | None = None,
) -> dict:
    chapter = {
        "photo_id": "301",
        "index": 1,
        "title": "第 1 章",
        "dir_name": "第1章",
        "page_count": 1,
    }
    if version >= 2:
        chapter.update(
            {
                "image_format": "jpg",
                "downloaded_at_utc": "2026-01-01T00:00:00Z",
            }
        )
    if chapter_fields:
        chapter.update(chapter_fields)
    return {
        "version": version,
        "album_id": album_id,
        "album_title": "测试漫画",
        "album_dir_name": "测试漫画",
        "chapters": [chapter],
    }


def _pdf_readable(path: Path) -> bool:
    """Candidate lightweight offline PDF readability check (probe)."""

    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            handle.seek(max(0, size - 1024))
            tail = handle.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def _cbz_check(path: Path, expected_images: int) -> bool:
    """Candidate offline CBZ structural check (probe)."""

    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                return False
            names = [
                name
                for name in archive.namelist()
                if not name.endswith("/")
            ]
            if len(names) != expected_images:
                return False
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    return False
        return True
    except (OSError, zipfile.BadZipFile):
        return False


class V291ManifestFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.paths.pictures.mkdir(parents=True, exist_ok=True)
        self.store = ChapterManifestStore(self.paths)

    def _write_raw_manifest(self, payload: dict, album_id: str = "123"):
        path = self.paths.pictures / album_id / ".jm-chapters.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def test_v1_manifest_reads_without_disk_rewrite(self):
        path = self._write_raw_manifest(_manifest_payload(version=1))
        before = path.read_bytes()

        manifest = self.store.load("123")

        self.assertIsNotNone(manifest)
        (chapter,) = manifest.chapters
        self.assertIsNone(chapter.image_format)
        self.assertIsNone(chapter.downloaded_at_utc)
        self.assertEqual(path.read_bytes(), before)

    def test_v2_manifest_round_trip_keeps_all_fields(self):
        incoming = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    photo_id="301",
                    index=1,
                    title="第 1 章",
                    dir_name="第1章",
                    page_count=3,
                    image_format="png",
                    downloaded_at_utc="2026-01-02T03:04:05Z",
                ),
            ),
        )
        self.store.merge_and_save(incoming)

        raw = json.loads(
            (self.paths.pictures / "123" / ".jm-chapters.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(raw["chapters"][0]),
            {
                "photo_id",
                "index",
                "title",
                "dir_name",
                "page_count",
                "image_format",
                "downloaded_at_utc",
            },
        )
        loaded = self.store.load("123")
        self.assertEqual(loaded.chapters, incoming.chapters)

    def test_future_manifest_version_is_rejected_and_hidden(self):
        self._write_raw_manifest(
            _manifest_payload(version=CHAPTER_MANIFEST_SCHEMA_VERSION + 1)
        )
        with self.assertRaises(UnsupportedChapterManifestVersion):
            self.store.load("123")
        service = LibraryService(self.paths)
        with self.assertRaises(LibraryNotFound):
            service.get_item("123")

    def test_unknown_older_version_is_corrupt(self):
        self._write_raw_manifest(_manifest_payload(version=0))
        with self.assertRaises(CorruptChapterManifest):
            self.store.load("123")

    def test_chapter_dir_name_must_match_index(self):
        valid = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    photo_id="301",
                    index=1,
                    title="第 1 章",
                    dir_name="",
                    page_count=1,
                ),
                ChapterManifestEntry(
                    photo_id="302",
                    index=2,
                    title="第 2 章",
                    dir_name="第2章",
                    page_count=1,
                ),
            ),
        )
        self.store.merge_and_save(valid)

        invalid = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    photo_id="303",
                    index=3,
                    title="第 3 章",
                    dir_name="第4章",
                    page_count=1,
                ),
            ),
        )
        with self.assertRaises(ChapterManifestError):
            self.store.merge_and_save(invalid)

    def test_merge_preserves_untouched_chapter_entries(self):
        base = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    photo_id="301",
                    index=1,
                    title="第 1 章",
                    dir_name="第1章",
                    page_count=1,
                    image_format="jpg",
                    downloaded_at_utc="2026-01-01T00:00:00Z",
                ),
                ChapterManifestEntry(
                    photo_id="302",
                    index=2,
                    title="第 2 章",
                    dir_name="第2章",
                    page_count=1,
                    image_format="png",
                    downloaded_at_utc="2026-01-01T00:00:00Z",
                ),
            ),
        )
        self.store.merge_and_save(base)
        incoming = ChapterManifest(
            version=CHAPTER_MANIFEST_SCHEMA_VERSION,
            album_id="123",
            album_title="测试漫画",
            album_dir_name="测试漫画",
            chapters=(
                ChapterManifestEntry(
                    photo_id="303",
                    index=3,
                    title="第 3 章",
                    dir_name="第3章",
                    page_count=1,
                    image_format="jpg",
                    downloaded_at_utc="2026-01-02T00:00:00Z",
                ),
            ),
        )

        merged = self.store.merge_and_save(incoming)

        self.assertEqual(
            [chapter.photo_id for chapter in merged.chapters],
            ["301", "302", "303"],
        )
        self.assertEqual(merged.chapters[0], base.chapters[0])
        self.assertEqual(merged.chapters[1], base.chapters[1])


class V291ArtifactNamingFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)

    def _save_manifest(self, chapters) -> None:
        ChapterManifestStore(self.paths).merge_and_save(
            ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=chapters,
            )
        )

    def test_multi_chapter_artifacts_bind_by_dir_name(self):
        chapter_dir = self.paths.pictures / "123" / "测试漫画" / "第1章"
        _write_image(chapter_dir / "1.jpg")
        _write_image(chapter_dir / "2.jpg", "blue")
        package_dir = self.paths.pdfs / "123" / "测试漫画"
        package_dir.mkdir(parents=True)
        chapter_to_pdf(chapter_dir, package_dir / "第1章.pdf")
        chapter_to_cbz(chapter_dir, package_dir / "第1章.cbz")
        self._save_manifest(
            (
                ChapterManifestEntry(
                    photo_id="301",
                    index=1,
                    title="第 1 章",
                    dir_name="第1章",
                    page_count=2,
                    image_format="jpg",
                ),
            )
        )

        item = self.service.get_item("123")

        self.assertEqual(item.layout, LibraryLayout.MANAGED)
        self.assertTrue(item.has_pdf)
        self.assertTrue(item.has_cbz)
        self.assertEqual(item.pdf_directory, package_dir)
        self.assertEqual(item.cbz_directory, package_dir)

    def test_single_chapter_artifact_uses_album_dir_name(self):
        chapter_dir = self.paths.pictures / "123" / "测试漫画"
        _write_image(chapter_dir / "1.jpg")
        package_dir = self.paths.pdfs / "123" / "测试漫画"
        package_dir.mkdir(parents=True)
        chapter_to_pdf(chapter_dir, package_dir / "测试漫画.pdf")
        self._save_manifest(
            (
                ChapterManifestEntry(
                    photo_id="301",
                    index=1,
                    title="全一话",
                    dir_name="",
                    page_count=1,
                    image_format="jpg",
                ),
            )
        )

        item = self.service.get_item("123")

        self.assertTrue(item.has_pdf)
        self.assertIn(
            package_dir / "测试漫画.pdf",
            tuple(item.pdf_directory.iterdir()),
        )


class V291MutualExclusionFreezeTests(unittest.TestCase):
    def test_library_operation_conflicts_and_releases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = TaskManager(
                paths=AppPaths(Path(temp_dir)),
                max_concurrent=1,
            )
            try:
                manager.begin_library_operation("123")
                with self.assertRaises(TaskConflict):
                    manager.begin_library_operation("123")
                self.assertTrue(manager.is_library_operation_active("123"))
                manager.end_library_operation("123")
                self.assertFalse(manager.is_library_operation_active("123"))
                manager.begin_library_operation("123")
                manager.end_library_operation("123")
            finally:
                manager.shutdown()


class V291DeleteSafetyFreezeTests(unittest.TestCase):
    def test_delete_all_keeps_whole_pdf_and_leaves_no_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            service = LibraryService(paths)
            store = ChapterManifestStore(paths)
            store.merge_and_save(
                ChapterManifest(
                    version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                    album_id="123",
                    album_title="测试漫画",
                    album_dir_name="测试漫画",
                    chapters=(
                        ChapterManifestEntry(
                            photo_id="301",
                            index=1,
                            title="第 1 章",
                            dir_name="第1章",
                            page_count=1,
                            image_format="jpg",
                        ),
                    ),
                )
            )
            _write_image(paths.pictures / "123" / "测试漫画" / "第1章" / "1.jpg")
            package_dir = paths.pdfs / "123" / "测试漫画"
            package_dir.mkdir(parents=True)
            (package_dir / "第1章.pdf").write_bytes(b"pdf")
            whole_pdf = paths.pdfs / "123.pdf"
            whole_pdf.write_bytes(b"keep")

            service.delete_all("123")

            self.assertFalse((paths.pictures / "123").exists())
            self.assertFalse((paths.pdfs / "123").exists())
            self.assertEqual(whole_pdf.read_bytes(), b"keep")
            leftovers = [
                path
                for root in (paths.pictures, paths.pdfs)
                for path in root.iterdir()
                if ".delete" in path.name
            ]
            self.assertEqual(leftovers, [])


class V291ReadabilityProbeTests(unittest.TestCase):
    """Offline probes pinning the Phase 1 readability-check candidates."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.chapter_dir = self.root / "chapter"
        for index in range(3):
            _write_image(self.chapter_dir / f"{index + 1}.jpg")

    def test_pdf_probe_accepts_generated_and_rejects_truncated(self):
        pdf_path = chapter_to_pdf(self.chapter_dir, self.root / "out.pdf")
        self.assertIsNotNone(pdf_path)

        started = time.perf_counter()
        self.assertTrue(_pdf_readable(Path(pdf_path)))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)

        truncated = self.root / "truncated.pdf"
        data = Path(pdf_path).read_bytes()
        truncated.write_bytes(data[: len(data) // 2])
        self.assertFalse(_pdf_readable(truncated))
        self.assertFalse(_pdf_readable(self.root / "missing.pdf"))

    def test_cbz_probe_validates_structure_and_entry_names(self):
        cbz_path = chapter_to_cbz(self.chapter_dir, self.root / "out.cbz")

        started = time.perf_counter()
        self.assertTrue(_cbz_check(cbz_path, expected_images=3))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertFalse(_cbz_check(cbz_path, expected_images=2))

        truncated = self.root / "truncated.cbz"
        data = cbz_path.read_bytes()
        truncated.write_bytes(data[: len(data) // 2])
        self.assertFalse(_cbz_check(truncated, expected_images=3))

        evil = self.root / "evil.cbz"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("1.jpg", b"x")
            archive.writestr("../evil.jpg", b"x")
        self.assertFalse(_cbz_check(evil, expected_images=2))


class V291InterfaceSurfaceTests(unittest.TestCase):
    def test_manifest_entry_keeps_v2_fields(self):
        import dataclasses

        names = {field.name for field in dataclasses.fields(ChapterManifestEntry)}
        self.assertTrue(
            {
                "photo_id",
                "index",
                "title",
                "dir_name",
                "page_count",
                "image_format",
                "downloaded_at_utc",
            }.issubset(names)
        )

    def test_library_service_surface_is_stable(self):
        for name in (
            "list_items",
            "get_item",
            "completed_chapter_ids",
            "delete_images",
            "delete_pdf",
            "delete_packaged_artifacts",
            "delete_all",
            "open_location",
            "get_preview",
            "get_pdf_directory",
        ):
            self.assertTrue(
                callable(getattr(LibraryService, name, None)),
                name,
            )


class V291TrackRegistryTests(unittest.TestCase):
    def test_six_tracks_and_two_user_decisions_are_explicit(self):
        self.assertEqual(
            set(V291_TRACKS),
            {
                "manifest_v3",
                "chapter_status",
                "rebuild",
                "chapter_delete",
                "selective_repair",
                "legacy_migration",
            },
        )
        self.assertEqual(
            V291_USER_DECISIONS,
            {
                "album_delete_images": "preserve_chapter_entries",
                "whole_pdf_removal": "user_entrypoint_only",
            },
        )


if __name__ == "__main__":
    unittest.main()
