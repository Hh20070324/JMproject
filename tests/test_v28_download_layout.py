import shutil
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jm_downloader import downloader, library
from jm_downloader.settings import AppPaths


class FakePhoto:
    def __init__(self, album, photo_id, index, title, page_count=2):
        self.from_album = album
        self.photo_id = str(photo_id)
        self.id = str(photo_id)
        self.album_index = index
        self.title = title
        self.name = title
        self._page_count = page_count

    def __len__(self):
        return self._page_count


class FakeAlbum:
    def __init__(self, album_id, title, chapter_specs):
        self.id = str(album_id)
        self.album_id = str(album_id)
        self.title = title
        self.name = title
        self.cover = None
        self.page_count = 0
        self._photos = tuple(
            FakePhoto(self, photo_id, index, chapter_title)
            for photo_id, index, chapter_title in chapter_specs
        )

    def __len__(self):
        return len(self._photos)

    def __iter__(self):
        return iter(self._photos)

    def is_album(self):
        return True


class V28DownloadLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        shutil.copy2(
            Path(__file__).resolve().parents[1] / "option.yml",
            self.paths.option_file,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _option(self, worker):
        return worker._make_option()

    @staticmethod
    def _active():
        return SimpleNamespace(client=Mock())

    def test_single_album_omits_chapter_directory(self):
        worker = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("301",),
        )
        album = FakeAlbum("123", "单章漫画", (("301", 1, "单章"),))
        worker._prepare_album(album)
        photos = worker._prepare_selected_photos(
            self._active(),
            album,
            tuple(album),
        )
        option = self._option(worker)

        self.assertEqual(
            Path(option.dir_rule.decide_image_save_dir(album, photos[0])),
            self.paths.pictures / "123" / "单章漫画",
        )
        published = worker._manifest_store.merge_and_save(
            worker._pending_manifest
        )
        self.assertEqual(published.chapters[0].dir_name, "")

    def test_selected_chapter_from_multi_album_keeps_numbered_directory(self):
        worker = downloader.DownloadWorker(
            "456",
            paths=self.paths,
            selected_chapter_ids=("402",),
        )
        album = FakeAlbum(
            "456",
            "多章漫画",
            (
                ("401", 1, "第一章"),
                ("402", 2, "第二章"),
            ),
        )
        worker._prepare_album(album)
        photos = worker._prepare_selected_photos(
            self._active(),
            album,
            tuple(album),
        )
        option = self._option(worker)

        self.assertEqual(
            Path(option.dir_rule.decide_image_save_dir(album, photos[0])),
            self.paths.pictures / "456" / "多章漫画" / "第2章",
        )
        self.assertEqual(worker._pending_manifest.chapters[0].photo_id, "402")

    def test_existing_manifest_pins_directory_across_remote_title_change(self):
        first = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("301",),
        )
        first_album = FakeAlbum("123", "原始标题", (("301", 1, "第一章"),))
        first._prepare_album(first_album)
        first._prepare_selected_photos(
            self._active(),
            first_album,
            tuple(first_album),
        )
        first._manifest_store.merge_and_save(first._pending_manifest)

        second = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("302",),
        )
        second_album = FakeAlbum(
            "123",
            "远端改名",
            (
                ("301", 1, "第一章"),
                ("302", 2, "第二章"),
            ),
        )
        second._prepare_album(second_album)
        second._prepare_selected_photos(
            self._active(),
            second_album,
            tuple(second_album),
        )

        self.assertEqual(second._album_dir_name, "原始标题")
        self.assertEqual(
            second_album.jm_downloader_album_dir,
            "原始标题",
        )
        self.assertFalse(
            (self.paths.pictures / "123" / "远端改名").exists()
        )

    def test_legacy_whole_album_task_stops_before_chapter_or_image_request(self):
        worker = downloader.DownloadWorker("123", paths=self.paths)
        album = FakeAlbum(
            "123",
            "旧任务",
            (
                ("301", 1, "第一章"),
                ("302", 2, "第二章"),
            ),
        )
        active = self._active()

        with self.assertRaises(downloader.LegacyChapterSelectionRequired):
            worker._prepare_album(album)

        active.client.check_photo.assert_not_called()

    def test_duplicate_chapter_index_is_rejected_before_manifest_publish(self):
        worker = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("301", "302"),
        )
        album = FakeAlbum(
            "123",
            "序号冲突",
            (
                ("301", 1, "第一章"),
                ("302", 1, "重复第一章"),
            ),
        )
        worker._prepare_album(album)

        with self.assertRaises(downloader.SelectedChapterUnavailable):
            worker._prepare_selected_photos(
                self._active(),
                album,
                tuple(album),
            )

        self.assertIsNone(worker._pending_manifest)
        self.assertIsNone(library.ChapterManifestStore(self.paths).load("123"))

    def test_corrupt_manifest_is_backed_up_and_republished_after_download(self):
        album_root = self.paths.pictures / "123"
        album_root.mkdir(parents=True)
        manifest_path = album_root / library.CHAPTER_MANIFEST_FILENAME
        manifest_path.write_bytes(b"{broken")
        worker = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("301",),
        )
        album = FakeAlbum("123", "恢复下载", (("301", 1, "第一章"),))

        worker._prepare_album(album)
        worker._prepare_selected_photos(
            self._active(),
            album,
            tuple(album),
        )
        published = worker._manifest_store.merge_and_save(
            worker._pending_manifest
        )

        self.assertEqual(published.album_title, "恢复下载")
        self.assertEqual(
            library.ChapterManifestStore(self.paths).load("123"),
            published,
        )
        backups = tuple(
            album_root.glob(
                f"{library.CHAPTER_MANIFEST_FILENAME}.corrupt-*"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"{broken")

    def test_dangling_manifest_symlink_is_rejected_before_chapter_request(self):
        album_root = self.paths.pictures / "123"
        album_root.mkdir(parents=True)
        manifest_path = album_root / library.CHAPTER_MANIFEST_FILENAME
        try:
            os.symlink(album_root / "missing.json", manifest_path)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"当前账户不能创建符号链接：{error}")
        worker = downloader.DownloadWorker(
            "123",
            paths=self.paths,
            selected_chapter_ids=("301",),
        )
        album = FakeAlbum("123", "链接拒绝", (("301", 1, "第一章"),))
        active = self._active()

        with self.assertRaises(library.ChapterManifestError):
            worker._prepare_album(album)

        active.client.check_photo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
