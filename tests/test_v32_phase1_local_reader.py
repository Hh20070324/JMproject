import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from jm_downloader.library import ChapterManifestStore
from jm_downloader.local_reader import LocalReaderService
from jm_downloader.models import (
    ChapterManifest,
    ChapterManifestEntry,
    ReaderErrorKind,
    ReaderPageState,
)
from jm_downloader.reader import ReaderServiceError
from jm_downloader.settings import AppPaths


class LocalReaderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = AppPaths(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _image(self, path: Path, *, size=(32, 48), color="white") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path, format="JPEG")

    def _manifest(self, chapters) -> None:
        ChapterManifestStore(self.paths).replace_exact(
            ChapterManifest(
                version=3,
                album_id="123",
                album_title="本地漫画",
                album_dir_name="本地漫画",
                chapters=tuple(chapters),
            )
        )

    @staticmethod
    def _chapter(photo_id, index, name, page_count, image_format="jpg"):
        return ChapterManifestEntry(
            photo_id=str(photo_id),
            index=index,
            title=name,
            dir_name=name,
            page_count=page_count,
            image_format=image_format,
            package_format="images",
        )

    async def test_filters_incomplete_chapters_and_uses_natural_page_order(self):
        complete = self.root / "Pictures" / "123" / "本地漫画" / "第1章"
        self._image(complete / "10.jpg", color="red")
        self._image(complete / "2.jpg", color="blue")
        self._manifest(
            (
                self._chapter("301", 1, "第1章", 2),
                self._chapter("302", 2, "第2章", 1),
            )
        )
        service = LocalReaderService(self.paths)

        catalog = await service.fetch_catalog("123")
        chapter, pages = await service.load_chapter(catalog, "301")
        _, first = await service.fetch_page(
            "301", 1, current_page=1, pinned_keys=()
        )

        self.assertEqual([item.photo_id for item in catalog.chapters], ["301"])
        self.assertTrue(catalog.chapters[0].downloaded)
        self.assertEqual(chapter.page_count, 2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(first.cache_path.name, "2.jpg")
        self.assertEqual(first.state, ReaderPageState.READY)
        self.assertEqual((first.width, first.height), (32, 48))
        self.assertFalse(self.paths.reader_temp.exists())

    async def test_deleted_or_replaced_page_fails_without_following_new_path(self):
        chapter_dir = (
            self.root / "Pictures" / "123" / "本地漫画" / "第1章"
        )
        page = chapter_dir / "1.jpg"
        self._image(page)
        self._manifest((self._chapter("301", 1, "第1章", 1),))
        service = LocalReaderService(self.paths)
        catalog = await service.fetch_catalog("123")
        await service.load_chapter(catalog, "301")

        page.unlink()
        with self.assertRaises(ReaderServiceError) as deleted:
            await service.fetch_page("301", 1, current_page=1)
        self.assertEqual(deleted.exception.kind, ReaderErrorKind.IMAGE_DAMAGED)

        self._image(page, size=(64, 64), color="black")
        with self.assertRaises(ReaderServiceError) as replaced:
            await service.fetch_page("301", 1, current_page=1)
        self.assertEqual(replaced.exception.kind, ReaderErrorKind.IMAGE_DAMAGED)

    async def test_damaged_and_wrong_format_chapters_are_not_readable(self):
        chapter_dir = (
            self.root / "Pictures" / "123" / "本地漫画" / "第1章"
        )
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "1.jpg").write_bytes(b"not-an-image")
        wrong_format = (
            self.root / "Pictures" / "123" / "本地漫画" / "第2章"
        )
        self._image(wrong_format / "1.jpg")
        self._manifest(
            (
                self._chapter("301", 1, "第1章", 1, "jpg"),
                self._chapter("302", 2, "第2章", 1, "png"),
            )
        )

        with self.assertRaises(ReaderServiceError) as raised:
            await LocalReaderService(self.paths).fetch_catalog("123")

        self.assertEqual(
            raised.exception.kind,
            ReaderErrorKind.CHAPTER_UNAVAILABLE,
        )

    async def test_close_releases_mappings_without_deleting_files(self):
        page = (
            self.root
            / "Pictures"
            / "123"
            / "本地漫画"
            / "第1章"
            / "1.jpg"
        )
        self._image(page)
        self._manifest((self._chapter("301", 1, "第1章", 1),))
        service = LocalReaderService(self.paths)
        await service.fetch_catalog("123")

        self.assertTrue(await service.close())
        self.assertTrue(page.is_file())
        with self.assertRaises(ReaderServiceError):
            await service.fetch_catalog("123")

    async def test_construction_and_failed_open_do_not_create_local_files(self):
        empty_root = self.root / "empty-portable-root"
        paths = AppPaths(empty_root)
        service = LocalReaderService(paths)

        self.assertFalse(empty_root.exists())
        with self.assertRaises(ReaderServiceError):
            await service.fetch_catalog("123")

        self.assertFalse(empty_root.exists())
        self.assertFalse(paths.reader_temp.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    async def test_chapter_junction_outside_managed_root_is_rejected(self):
        title_dir = self.root / "Pictures" / "123" / "本地漫画"
        title_dir.mkdir(parents=True)
        outside = self.root / "outside"
        self._image(outside / "1.jpg")
        junction = title_dir / "第1章"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前环境无法创建 Windows junction")
        try:
            self._manifest((self._chapter("301", 1, "第1章", 1),))
            with self.assertRaises(ReaderServiceError):
                await LocalReaderService(self.paths).fetch_catalog("123")
        finally:
            if junction.exists():
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
