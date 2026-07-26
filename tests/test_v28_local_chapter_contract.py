import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from jm_downloader import downloader, library, models, pdf, settings, task_store, tasks
from jm_downloader.settings import AppPaths


def _require(module, name):
    value = getattr(module, name, None)
    if value is None:
        raise AssertionError(f"{module.__name__}.{name} is required by v2.8.0")
    return value


def _chapter(photo_id, index, title, dir_name, page_count):
    entry_type = _require(models, "ChapterManifestEntry")
    return entry_type(
        photo_id=photo_id,
        index=index,
        title=title,
        dir_name=dir_name,
        page_count=page_count,
    )


def _manifest(
    album_id="123",
    title="测试漫画",
    directory="测试漫画",
    chapters=None,
):
    manifest_type = _require(models, "ChapterManifest")
    if chapters is None:
        chapters = (
            _chapter("301", 1, "第一章", "第1章", 2),
            _chapter("302", 2, "第二章", "第2章", 3),
        )
    return manifest_type(
        version=1,
        album_id=album_id,
        album_title=title,
        album_dir_name=directory,
        chapters=tuple(chapters),
    )


class ChapterSelectionLimitContractTests(unittest.TestCase):
    def test_shared_limit_is_ten_and_business_layer_rejects_eleven(self):
        limit = _require(models, "MAX_CHAPTERS_PER_TASK")
        self.assertEqual(limit, 10)
        accepted = tuple(str(index) for index in range(1, limit + 1))
        self.assertEqual(tasks.normalize_selected_chapter_ids(accepted), accepted)

        with self.assertRaisesRegex(tasks.InvalidChapterSelection, "最多.*10"):
            tasks.normalize_selected_chapter_ids(accepted + ("11",))

    def test_task_store_rejects_oversized_explicit_selection_but_keeps_legacy_none(self):
        values = dict(
            id="task-1",
            album_id="123",
            title="测试漫画",
            status=models.TaskStatus.PAUSED,
            progress=20,
            chapter="",
            page="",
            error=None,
            pictures_directory="Pictures",
            pdf_directory="PDFs",
        )
        task_store.StoredTask(selected_chapter_ids=None, **values).validate()
        with self.assertRaisesRegex(task_store.TaskStoreValidationError, "最多.*10"):
            task_store.StoredTask(
                selected_chapter_ids=tuple(str(index) for index in range(1, 12)),
                **values,
            ).validate()

    def test_worker_defensively_rejects_oversized_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "at most 10|最多.*10"):
                downloader.DownloadWorker(
                    "123",
                    paths=AppPaths(Path(temp_dir)),
                    selected_chapter_ids=tuple(
                        str(index) for index in range(1, 12)
                    ),
                )

    def test_parallel_and_queued_map_to_bounded_photo_concurrency(self):
        for behavior, expected in (("parallel", 2), ("queued", 1)):
            with self.subTest(behavior=behavior):
                option = Mock()
                with tempfile.TemporaryDirectory() as temp_dir:
                    worker = downloader.DownloadWorker(
                        "123",
                        paths=AppPaths(Path(temp_dir)),
                        multi_chapter_download_behavior=behavior,
                    )
                    with (
                        patch.object(
                            downloader.jmcomic,
                            "create_option_by_file",
                            return_value=option,
                        ),
                        patch.object(
                            downloader,
                            "install_safe_jmcomic_logging",
                        ),
                    ):
                        worker._make_option()
                self.assertEqual(option.download.threading.photo, expected)

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "parallel|queued"):
                downloader.DownloadWorker(
                    "123",
                    paths=AppPaths(Path(temp_dir)),
                    multi_chapter_download_behavior="cpu",
                )


class ChapterManifestContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.paths.ensure_output_directories()
        store_type = _require(library, "ChapterManifestStore")
        self.store = store_type(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_manifest_models_are_frozen_slotted_values(self):
        manifest = _manifest()
        self.assertEqual(
            [field.name for field in fields(manifest)],
            [
                "version",
                "album_id",
                "album_title",
                "album_dir_name",
                "chapters",
            ],
        )
        self.assertIsInstance(manifest.chapters, tuple)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            manifest.album_title = "changed"
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            manifest.chapters[0].title = "changed"

    def test_manifest_round_trip_and_partial_merge_are_atomic(self):
        first = _manifest(chapters=(_chapter("301", 1, "第一章", "第1章", 2),))
        published = self.store.merge_and_save(first)
        self.assertEqual(published, replace(first, version=2))

        second = _manifest(
            title="远端新标题",
            directory="远端新标题",
            chapters=(_chapter("302", 2, "第二章", "第2章", 3),),
        )
        merged = self.store.merge_and_save(second)

        self.assertEqual(merged.album_title, "测试漫画")
        self.assertEqual(merged.album_dir_name, "测试漫画")
        self.assertEqual(
            tuple(chapter.photo_id for chapter in merged.chapters),
            ("301", "302"),
        )
        self.assertEqual(self.store.load("123"), merged)
        manifest_path = (
            self.paths.pictures / "123" / library.CHAPTER_MANIFEST_FILENAME
        )
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(
            list(manifest_path.parent.glob(f".{manifest_path.name}.*.tmp")),
            [],
        )

    def test_corrupt_manifest_is_preserved_before_current_facts_are_published(self):
        album_root = self.paths.pictures / "123"
        album_root.mkdir(parents=True)
        manifest_path = album_root / library.CHAPTER_MANIFEST_FILENAME
        raw = b'{"version": 1, broken'
        manifest_path.write_bytes(raw)

        published = self.store.merge_and_save(
            _manifest(chapters=(_chapter("302", 2, "第二章", "第2章", 3),))
        )

        backups = list(album_root.glob(".jm-chapters.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)
        self.assertEqual(published.chapters[0].photo_id, "302")

    def test_failed_manifest_replace_keeps_previous_manifest(self):
        original = self.store.merge_and_save(
            _manifest(chapters=(_chapter("301", 1, "第一章", "第1章", 2),))
        )
        manifest_path = (
            self.paths.pictures / "123" / library.CHAPTER_MANIFEST_FILENAME
        )
        before = manifest_path.read_bytes()

        with patch.object(
            library.os,
            "replace",
            side_effect=PermissionError("locked"),
        ):
            with self.assertRaisesRegex(library.LibraryError, "清单"):
                self.store.merge_and_save(
                    _manifest(
                        chapters=(
                            _chapter("302", 2, "第二章", "第2章", 3),
                        )
                    )
                )

        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(self.store.load("123"), original)


class AlbumDirectoryContractTests(unittest.TestCase):
    def test_title_sanitizer_handles_windows_names_edges_and_empty_fallback(self):
        sanitize = _require(downloader, "sanitize_album_directory_name")
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            for title in ("CON", "nul.txt", " AUX. ", 'a<b>c:d"e/f\\g|h?'):
                with self.subTest(title=title):
                    value = sanitize(title, "123", paths)
                    self.assertTrue(value)
                    self.assertNotIn(value.upper().split(".")[0], {
                        "CON",
                        "PRN",
                        "AUX",
                        "NUL",
                    })
                    self.assertFalse(value.endswith((" ", ".")))
                    self.assertFalse(any(char in value for char in '<>:"/\\|?*'))
            self.assertEqual(sanitize(" . ", "123", paths), "123")

    def test_title_sanitizer_respects_the_shorter_dynamic_output_budget(self):
        sanitize = _require(downloader, "sanitize_album_directory_name")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            short_paths = AppPaths(root)
            long_paths = AppPaths(
                root,
                pictures_override=root / ("p" * 80),
                pdfs_override=root / ("d" * 100),
            )
            title = "很长的漫画标题" * 80
            short_value = sanitize(title, "123", short_paths)
            long_value = sanitize(title, "123", long_paths)

        self.assertGreater(len(short_value), len(long_value))
        self.assertTrue(long_value)

    def test_empty_dir_rule_segment_collapses_for_pinned_jmcomic(self):
        class Entity:
            def __init__(self, values):
                self.values = values

            def get_properties_dict(self):
                return dict(self.values)

        with tempfile.TemporaryDirectory() as temp_dir:
            rule = downloader.jmcomic.DirRule(
                "Bd/{Aid}/{Aalbum_dir}/{Pchapter_dir}",
                base_dir=temp_dir,
            )
            album = Entity({"Aid": "123", "Aalbum_dir": "漫画名"})
            single = Entity({"Pchapter_dir": ""})
            multiple = Entity({"Pchapter_dir": "第2章"})

            self.assertEqual(
                Path(rule.apply_rule_to_path(album, single)),
                Path(temp_dir) / "123" / "漫画名",
            )
            self.assertEqual(
                Path(rule.apply_rule_to_path(album, multiple)),
                Path(temp_dir) / "123" / "漫画名" / "第2章",
            )


class ChapterPdfContractTests(unittest.TestCase):
    def test_chapter_pdf_uses_exact_output_file_and_atomic_replacement(self):
        chapter_to_pdf = _require(pdf, "chapter_to_pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter = root / "Pictures" / "123" / "漫画" / "第2章"
            chapter.mkdir(parents=True)
            Image.new("RGB", (4, 4), "white").save(chapter / "2.jpg", "JPEG")
            Image.new("RGB", (4, 4), "black").save(chapter / "10.jpg", "JPEG")
            output = root / "PDFs" / "123" / "漫画" / "第2章.pdf"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"old")

            result = chapter_to_pdf(
                chapter,
                output,
                publish_guard=lambda: True,
            )

            self.assertEqual(Path(result), output)
            self.assertGreater(output.stat().st_size, 3)
            self.assertEqual(list(output.parent.glob("*.pdf.part")), [])


class SettingsContractTests(unittest.TestCase):
    def test_multi_chapter_behavior_defaults_validates_and_round_trips(self):
        defaults = settings.AppSettings()
        self.assertEqual(defaults.multi_chapter_download_behavior, "parallel")
        payload = defaults.to_dict()
        self.assertEqual(
            payload["download"]["multi_chapter_download_behavior"],
            "parallel",
        )
        self.assertEqual(
            settings.AppSettings.from_dict({}).multi_chapter_download_behavior,
            "parallel",
        )
        queued_payload = defaults.to_dict()
        queued_payload["download"]["multi_chapter_download_behavior"] = "queued"
        self.assertEqual(
            settings.AppSettings.from_dict(
                queued_payload
            ).multi_chapter_download_behavior,
            "queued",
        )
        invalid = defaults.to_dict()
        invalid["download"]["multi_chapter_download_behavior"] = "cpu"
        with self.assertRaises(settings.SettingsValidationError):
            settings.AppSettings.from_dict(invalid)


class LibraryLayoutContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = library.LibraryService(self.paths)
        store_type = _require(library, "ChapterManifestStore")
        self.manifests = store_type(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, relative, data=b"x"):
        path = self.paths.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_managed_legacy_and_unverified_pdf_only_are_distinct(self):
        managed = _manifest()
        self.manifests.merge_and_save(managed)
        self._write("Pictures/123/测试漫画/第1章/1.jpg")
        self._write("PDFs/123/测试漫画/第1章.pdf", b"managed-pdf")
        self._write("Pictures/456/旧章节/1.jpg")
        self._write("PDFs/789/未知标题/第1章.pdf", b"unknown-pdf")
        self._write("PDFs/999.pdf", b"legacy-whole-pdf")

        items = {item.album_id: item for item in self.service.list_items()}
        layout_type = _require(models, "LibraryLayout")

        self.assertEqual(set(items), {"123", "456", "789"})
        self.assertEqual(items["123"].layout, layout_type.MANAGED)
        self.assertEqual(items["123"].title, "测试漫画")
        self.assertEqual(
            items["123"].pdf_directory,
            self.paths.pdfs / "123" / "测试漫画",
        )
        self.assertEqual(items["456"].layout, layout_type.LEGACY)
        self.assertIsNone(items["456"].pdf_directory)
        self.assertEqual(items["789"].layout, layout_type.UNVERIFIED)
        self.assertIsNone(items["789"].title)
        self.assertEqual(items["789"].pdf_directory, self.paths.pdfs / "789")

    def test_images_and_pdf_delete_independently_and_ignore_legacy_whole_pdf(self):
        self.manifests.merge_and_save(_manifest())
        self._write("Pictures/123/测试漫画/第1章/1.jpg")
        self._write("PDFs/123/测试漫画/第1章.pdf", b"chapter-pdf")
        legacy = self._write("PDFs/123.pdf", b"keep-me")

        self.service.delete_images("123")
        item = self.service.get_item("123")
        self.assertFalse(item.has_images)
        self.assertTrue(item.has_pdf)
        self.assertEqual(item.chapter_count, 0)
        self.assertEqual(legacy.read_bytes(), b"keep-me")

        self.service.delete_pdf("123")
        self.assertFalse((self.paths.pdfs / "123").exists())
        self.assertEqual(legacy.read_bytes(), b"keep-me")

        with self.assertRaises(library.LibraryNotFound):
            self.service.get_item("123")

    def test_delete_all_rolls_back_both_managed_directories(self):
        self.manifests.merge_and_save(_manifest())
        image = self._write("Pictures/123/测试漫画/第1章/1.jpg", b"image")
        chapter_pdf = self._write(
            "PDFs/123/测试漫画/第1章.pdf",
            b"chapter-pdf",
        )
        legacy = self._write("PDFs/123.pdf", b"keep-me")
        pdf_root = self.paths.pdfs / "123"
        original_replace = library.os.replace

        def fail_pdf_stage(source, destination):
            if Path(source) == pdf_root:
                raise PermissionError("locked")
            return original_replace(source, destination)

        with patch.object(library.os, "replace", side_effect=fail_pdf_stage):
            with self.assertRaises(library.LibraryError):
                self.service.delete_all("123")

        self.assertEqual(image.read_bytes(), b"image")
        self.assertEqual(chapter_pdf.read_bytes(), b"chapter-pdf")
        self.assertEqual(legacy.read_bytes(), b"keep-me")


class TaskSnapshotContractTests(unittest.TestCase):
    def test_task_snapshot_uses_pdf_directory_not_pdf_path(self):
        names = {field.name for field in fields(models.TaskSnapshot)}
        self.assertIn("pdf_directory", names)
        self.assertNotIn("pdf_path", names)


if __name__ == "__main__":
    unittest.main()
