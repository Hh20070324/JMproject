import asyncio
import base64
import os
from pathlib import Path
import tempfile
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.library import ChapterManifestStore
from jm_downloader.local_reader import LocalReaderService
from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterManifest,
    ChapterManifestEntry,
    ChapterSnapshot,
    LibraryItem,
    LibraryLayout,
    ReaderChapterSnapshot,
    ReaderContentMode,
    ReaderErrorKind,
    ReaderPageSnapshot,
    ReaderPageState,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.reader_window import ReaderWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.library_item_card import LibraryItemCard
from jm_downloader.reader import ReaderServiceError
from jm_downloader.settings import AppPaths


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
    "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class LocalReaderMutationReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = AppPaths(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _image(self, chapter: str, name: str, color: str) -> Path:
        path = (
            self.root
            / "Pictures"
            / "123"
            / "本地漫画"
            / chapter
            / name
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 36), color).save(path, format="JPEG")
        return path

    @staticmethod
    def _entry(photo_id: str, index: int, title: str, pages: int):
        return ChapterManifestEntry(
            photo_id=photo_id,
            index=index,
            title=title,
            dir_name=title,
            page_count=pages,
            image_format="jpg",
            package_format="images",
        )

    async def test_rescan_excludes_deleted_chapter_and_keeps_survivor(self):
        first_page = self._image("第1章", "1.jpg", "red")
        self._image("第1章", "2.jpg", "blue")
        survivor = self._image("第3章", "1.jpg", "green")
        ChapterManifestStore(self.paths).replace_exact(
            ChapterManifest(
                version=3,
                album_id="123",
                album_title="本地漫画",
                album_dir_name="本地漫画",
                chapters=(
                    self._entry("301", 1, "第1章", 2),
                    self._entry("302", 2, "第2章", 1),
                    self._entry("303", 3, "第3章", 1),
                ),
            )
        )
        service = LocalReaderService(self.paths)

        original = await service.fetch_catalog("123")
        await service.load_chapter(original, "301")
        self.assertEqual(
            [chapter.photo_id for chapter in original.chapters],
            ["301", "303"],
        )

        first_page.unlink()
        with self.assertRaises(ReaderServiceError) as deleted:
            await service.fetch_page("301", 1, current_page=1)
        self.assertEqual(deleted.exception.kind, ReaderErrorKind.IMAGE_DAMAGED)

        rescanned = await service.fetch_catalog("123")
        self.assertEqual(
            [chapter.photo_id for chapter in rescanned.chapters],
            ["303"],
        )
        chapter, _pages = await service.load_chapter(rescanned, "303")
        _key, page = await service.fetch_page("303", 1, current_page=1)
        self.assertEqual(chapter.index, 3)
        self.assertEqual(page.cache_path, survivor)
        self.assertTrue(survivor.is_file())
        self.assertFalse(self.paths.reader_temp.exists())


class DelayedSourceService:
    def __init__(self, root: Path, name: str, *, page_delay: float = 0.0):
        self.root = root
        self.name = name
        self.page_delay = page_delay
        self.calls = []
        self.closed = False

    async def fetch_catalog(self, album_id):
        self.calls.append(("catalog", str(album_id)))
        return ChapterCatalogSnapshot(
            str(int(album_id)),
            f"{self.name}漫画",
            (ChapterSnapshot("301", 1, "第1章", downloaded=True),),
        )

    async def load_chapter(self, _catalog, photo_id):
        self.calls.append(("chapter", photo_id))
        return (
            ReaderChapterSnapshot(photo_id, 1, "第1章", 1),
            (
                ReaderPageSnapshot(
                    photo_id,
                    1,
                    1,
                    ReaderPageState.PLACEHOLDER,
                ),
            ),
        )

    async def fetch_page(
        self,
        photo_id,
        page_number,
        *,
        current_page,
        pinned_keys,
    ):
        del current_page, pinned_keys
        self.calls.append(("page", photo_id, page_number))
        if self.page_delay:
            await asyncio.sleep(self.page_delay)
        path = self.root / f"{self.name}-{photo_id}-{page_number}.png"
        path.write_bytes(PNG)
        return (
            path.name,
            ReaderPageSnapshot(
                photo_id,
                page_number,
                1,
                ReaderPageState.READY,
                width=1,
                height=1,
                cache_path=path,
            ),
        )

    async def close(self):
        self.closed = True
        return True


class ReaderSourceSwitchReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.online = DelayedSourceService(self.root, "online")
        self.local = DelayedSourceService(
            self.root,
            "local",
            page_delay=0.12,
        )
        self.controller = ReaderController(
            self.online,
            local_service=self.local,
            result_interval_ms=5,
        )

    def tearDown(self):
        self.controller.shutdown(2.0)
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_late_local_page_is_discarded_after_online_switch(self):
        catalogs = []
        chapters = []
        pages = []
        self.controller.catalog_ready.connect(
            lambda generation, catalog: catalogs.append((generation, catalog))
        )
        self.controller.chapter_ready.connect(
            lambda generation, chapter, values: chapters.append(
                (generation, chapter, values)
            )
        )
        self.controller.page_ready.connect(
            lambda generation, snapshot, _image: pages.append(
                (generation, snapshot)
            )
        )

        local_open = self.controller.open_album(
            "123",
            content_mode=ReaderContentMode.LOCAL,
        )
        self.assertTrue(self._wait(lambda: len(catalogs) == 1))
        local_chapter = self.controller.load_chapter(
            catalogs[0][1],
            "301",
            target_width=64,
        )
        self.assertTrue(self._wait(lambda: len(chapters) == 1))
        self.assertTrue(
            self._wait(
                lambda: any(call[0] == "page" for call in self.local.calls)
            )
        )

        online_open = self.controller.open_album(
            "456",
            content_mode=ReaderContentMode.ONLINE,
        )
        self.assertTrue(self._wait(lambda: len(catalogs) == 2))
        time.sleep(0.16)
        self.app.processEvents()

        self.assertNotEqual(local_open, local_chapter)
        self.assertNotEqual(local_chapter, online_open)
        self.assertEqual(pages, [])
        self.assertEqual(catalogs[-1][0], online_open)
        self.assertEqual(catalogs[-1][1].title, "online漫画")

        online_chapter = self.controller.load_chapter(
            catalogs[-1][1],
            "301",
            target_width=64,
        )
        self.assertTrue(self._wait(lambda: bool(pages)))
        self.assertEqual(pages, [(online_chapter, pages[0][1])])
        self.assertIn("online-", pages[0][1].cache_path.name)
        self.assertGreaterEqual(self.controller.maximum_network_active, 1)


class ReaderAndLibraryUiReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_reader_window_cycles_alternating_content_modes(self):
        service = DelayedSourceService(Path(tempfile.gettempdir()), "idle")
        controller = ReaderController(service, result_interval_ms=5)
        window = ReaderWindow(controller, persist_geometry=False)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        initial_generation = controller.generation
        try:
            for index in range(20):
                mode = (
                    ReaderContentMode.LOCAL
                    if index % 2
                    else ReaderContentMode.ONLINE
                )
                window.begin_session(str(100 + index), f"漫画 {index}", mode)
                self.app.processEvents()
                self.assertEqual(window.session_content_mode, mode)
                self.assertTrue(window.isVisible())
                self.assertFalse(window.isModal())
                window.close()
                self.app.processEvents()
                self.assertFalse(window.has_session)
                self.assertIsNone(window.session_content_mode)
            self.assertGreaterEqual(
                controller.generation,
                initial_generation + 20,
            )
        finally:
            window.close()
            window.deleteLater()
            controller.shutdown(2.0)
            controller.deleteLater()
            self.app.processEvents()

    def test_local_card_actions_stay_inside_card_across_themes_and_widths(self):
        with tempfile.TemporaryDirectory() as directory:
            package_dir = Path(directory)
            item = LibraryItem(
                album_id="123",
                title="本地漫画",
                layout=LibraryLayout.MANAGED,
                chapter_count=3,
                image_count=10,
                image_size=1_024,
                preview_path=None,
                pdf_directory=package_dir,
                pdf_size=512,
            )
            previous_stylesheet = self.app.styleSheet()
            try:
                for theme in ("light", "dark"):
                    ThemeManager(theme).apply()
                    for width in (340, 425, 510, 680):
                        card = LibraryItemCard(item)
                        card.resize(width, card.height())
                        card.show()
                        self.app.processEvents()
                        buttons = tuple(
                            button
                            for button in (
                                card.open_images_button,
                                card.read_button,
                                card.open_pdf_button,
                                card.view_task_button,
                                card.chapter_button,
                                card.delete_button,
                            )
                            if button.isVisible()
                        )
                        rects = []
                        for button in buttons:
                            top_left = button.mapTo(card, QPoint(0, 0))
                            rect = button.rect().translated(top_left)
                            self.assertTrue(card.contentsRect().contains(rect))
                            rects.append(rect)
                        for index, rect in enumerate(rects):
                            for other in rects[index + 1 :]:
                                self.assertFalse(rect.intersects(other))
                        card.close()
                        card.deleteLater()
                        self.app.processEvents()
            finally:
                self.app.setStyleSheet(previous_stylesheet)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
