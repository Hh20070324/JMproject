"""v2.9.1 Phase 4: offline repair planning and configured redownload tasks."""

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from jm_downloader.library import (
    CHAPTER_MANIFEST_SCHEMA_VERSION,
    ChapterManifestStore,
    LibraryService,
)
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    ChapterRepairBatch,
    TaskConfig,
)
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskBatchValidationError, TaskManager


def _write_image(path: Path, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 4), color).save(path)
    return path


def _entry(
    photo_id: str,
    index: int,
    *,
    package_format: str | None,
) -> ChapterManifestEntry:
    return ChapterManifestEntry(
        photo_id=photo_id,
        index=index,
        title=f"第 {index} 章",
        dir_name=f"第{index}章",
        page_count=1,
        image_format="jpg",
        downloaded_at_utc="2026-07-26T00:00:00Z",
        package_format=package_format,
    )


class PassiveWorker:
    def __init__(self, album_id, **_kwargs):
        self.album_id = album_id

    def start(self):
        return None

    def stop(self):
        return None

    def wait(self, _timeout=None):
        return True


class RepairPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.service = LibraryService(self.paths)
        self.store = ChapterManifestStore(self.paths)

    def _save(self, chapters) -> Path:
        self.store.merge_and_save(
            ChapterManifest(
                version=CHAPTER_MANIFEST_SCHEMA_VERSION,
                album_id="123",
                album_title="测试漫画",
                album_dir_name="测试漫画",
                chapters=tuple(chapters),
            )
        )
        return self.paths.pictures / "123" / ".jm-chapters.json"

    def _image(self, index: int, *, name: str = "1.jpg") -> Path:
        return _write_image(
            self.paths.pictures
            / "123"
            / "测试漫画"
            / f"第{index}章"
            / name,
        )

    def test_offline_plan_separates_rebuild_redownload_and_unchanged(self):
        self._image(1)  # complete PDF images, package missing -> rebuild
        # chapter 2 has no images -> CBZ redownload
        self._image(3)
        self._image(3, name="2.jpg")  # count mismatch -> damaged/redownload
        self._image(4)  # images-only and complete -> unchanged
        self._image(5)  # unknown but confirmed PDF -> rebuild + persist later
        # chapter 6 unknown and missing choice -> explicit failure
        manifest_path = self._save(
            [
                _entry("301", 1, package_format="pdf"),
                _entry("302", 2, package_format="cbz"),
                _entry("303", 3, package_format="images"),
                _entry("304", 4, package_format="images"),
                _entry("305", 5, package_format=None),
                _entry("306", 6, package_format=None),
            ]
        )
        before = manifest_path.read_bytes()

        plan = self.service.plan_chapter_repairs(
            "123",
            ("301", "302", "303", "304", "305", "306", "missing"),
            confirmed_formats={"305": "pdf"},
        )

        self.assertEqual(plan.rebuild_photo_ids, ("301", "305"))
        self.assertEqual(
            plan.download_batches,
            (
                ChapterRepairBatch("cbz", ("302",)),
                ChapterRepairBatch("images", ("303",)),
            ),
        )
        self.assertEqual(plan.unchanged_photo_ids, ("304",))
        self.assertEqual(
            [failure.photo_id for failure in plan.failures],
            ["306", "missing"],
        )
        self.assertEqual(plan.resolved_formats, (("305", "pdf"),))
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_local_rebuild_path_uses_plan_and_does_not_create_downloads(self):
        self._image(1)
        self._image(2)
        self._save(
            [
                _entry("301", 1, package_format="pdf"),
                _entry("302", 2, package_format=None),
            ]
        )
        plan = self.service.plan_chapter_repairs(
            "123",
            ("301", "302"),
            confirmed_formats={"302": "images"},
        )

        result = self.service.rebuild_chapters(
            "123",
            plan.rebuild_photo_ids,
            confirmed_formats=dict(plan.resolved_formats),
        )

        self.assertEqual(result.failures, ())
        self.assertEqual(
            [item.package_format for item in result.succeeded],
            ["pdf", "images"],
        )
        self.assertEqual(plan.download_batches, ())
        self.assertEqual(
            self.store.load("123").chapters[1].package_format,
            "images",
        )

    def test_unique_existing_package_infers_format_without_confirmation(self):
        self._image(1)
        package = self.paths.pdfs / "123" / "测试漫画" / "第1章.cbz"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(b"unique disk evidence")
        self._save([_entry("301", 1, package_format=None)])

        plan = self.service.plan_chapter_repairs("123", ("301",))

        self.assertEqual(plan.rebuild_photo_ids, ("301",))
        self.assertEqual(plan.failures, ())
        self.assertEqual(plan.resolved_formats, (("301", "cbz"),))


class RepairTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.manager = TaskManager(
            paths=self.paths,
            worker_factory=PassiveWorker,
        )
        self.addCleanup(lambda: self.manager.shutdown(timeout=1))

    def test_grouped_tasks_split_at_ten_and_keep_explicit_config(self):
        pdf_ids = tuple(str(1000 + index) for index in range(23))
        cbz_ids = ("2001", "2002")
        base = TaskConfig(
            download_engine="sync",
            api_route="www.cdngwc.club",
            package_format="images",
            image_format="png",
            image_concurrency=7,
            multi_chapter_download_behavior="queued",
        )

        created = self.manager.add_repair_batches(
            "123",
            (
                ChapterRepairBatch("pdf", pdf_ids),
                ChapterRepairBatch("cbz", cbz_ids),
            ),
            base_config=base,
        )

        self.assertEqual(len(created), 4)
        self.assertEqual(
            [len(task.selected_chapter_ids) for task in created],
            [10, 10, 3, 2],
        )
        self.assertEqual(
            [task.config.package_format for task in created],
            ["pdf", "pdf", "pdf", "cbz"],
        )
        for task in created:
            self.assertEqual(
                task.force_redownload_chapter_ids,
                task.selected_chapter_ids,
            )
            self.assertEqual(task.config.download_engine, "sync")
            self.assertEqual(task.config.api_route, "www.cdngwc.club")
            self.assertEqual(task.config.image_format, "png")
            self.assertEqual(task.config.image_concurrency, 7)
            self.assertEqual(
                task.config.multi_chapter_download_behavior,
                "queued",
            )

    def test_duplicate_format_groups_are_rejected_atomically(self):
        with self.assertRaises(TaskBatchValidationError):
            self.manager.add_repair_batches(
                "123",
                (
                    ChapterRepairBatch("pdf", ("301", "302")),
                    ChapterRepairBatch("cbz", ("302", "303")),
                ),
                base_config=TaskConfig(),
            )

        self.assertEqual(self.manager.list_tasks(), [])

    def test_existing_overlap_rejects_all_new_repair_groups(self):
        self.manager.add_batch(
            "123",
            selected_chapter_ids=("301",),
        )
        before = self.manager.list_tasks()

        with self.assertRaises(TaskBatchValidationError):
            self.manager.add_repair_batches(
                "123",
                (
                    ChapterRepairBatch("pdf", ("301",)),
                    ChapterRepairBatch("cbz", ("302",)),
                ),
                base_config=TaskConfig(),
            )

        self.assertEqual(self.manager.list_tasks(), before)


if __name__ == "__main__":
    unittest.main()
