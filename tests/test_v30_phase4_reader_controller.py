import base64
from pathlib import Path
import tempfile
import threading
import time
import unittest

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
    "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DeterministicProtector:
    def protect(self, plaintext):
        return b"reader:" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"reader:"):
            raise ValueError
        return ciphertext.removeprefix(b"reader:")[::-1]


class ControlledReaderService:
    def __init__(self, root: Path, delay=0.01):
        self.root = root
        self.delay = delay
        self.active = 0
        self.maximum_active = 0
        self.calls = []
        self.closed = False
        self.release = threading.Event()

    async def fetch_catalog(self, album_id):
        await self._wait()
        return ChapterCatalogSnapshot(
            str(int(album_id)),
            "测试漫画",
            (
                ChapterSnapshot("301", 1, "第 1 章"),
                ChapterSnapshot("302", 2, "第 2 章"),
            ),
        )

    async def load_chapter(self, catalog, photo_id):
        await self._wait()
        chapter = next(
            item for item in catalog.chapters if item.photo_id == photo_id
        )
        snapshot = ReaderChapterSnapshot(
            chapter.photo_id,
            chapter.index,
            chapter.title,
            100,
        )
        pages = tuple(
            ReaderPageSnapshot(
                photo_id,
                page,
                100,
                ReaderPageState.PLACEHOLDER,
            )
            for page in range(1, 101)
        )
        return snapshot, pages

    async def fetch_page(
        self,
        photo_id,
        page_number,
        *,
        current_page,
        pinned_keys,
    ):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.calls.append((photo_id, page_number, threading.get_ident()))
        try:
            await self._wait()
            path = self.root / f"{photo_id}-{page_number}.png"
            path.write_bytes(PNG)
            return (
                f"{photo_id}-{page_number}",
                ReaderPageSnapshot(
                    photo_id,
                    page_number,
                    100,
                    ReaderPageState.READY,
                    width=1,
                    height=1,
                    cache_path=path,
                ),
            )
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True
        return True

    async def _wait(self):
        import asyncio

        await asyncio.sleep(self.delay)


class ReaderControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["reader-controller-tests"]
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = ControlledReaderService(self.root)
        paths = AppPaths(self.root)
        self.history = ReaderHistoryStore(
            ProtectedStore.reading_history(
                paths,
                DeterministicProtector(),
            )
        )
        self.controller = ReaderController(
            self.service,
            history_store=self.history,
            result_interval_ms=5,
            history_debounce_ms=20,
            memory_budget_bytes=1024 * 1024,
        )

    def tearDown(self):
        self.assertTrue(self.controller.shutdown(2.0))
        self.controller.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    def load_first_chapter(self):
        catalogs = []
        chapters = []
        self.controller.catalog_ready.connect(
            lambda _generation, catalog: catalogs.append(catalog)
        )
        self.controller.chapter_ready.connect(
            lambda _generation, chapter, _pages: chapters.append(chapter)
        )
        self.controller.open_album("123")
        self.assertTrue(self.wait_until(lambda: len(catalogs) == 1))
        self.controller.load_chapter(
            catalogs[0],
            "301",
            target_width=800,
        )
        self.assertTrue(self.wait_until(lambda: len(chapters) == 1))
        return catalogs[0], chapters[0]

    def test_deduplicates_prioritizes_and_limits_network_to_three(self):
        _catalog, _chapter = self.load_first_chapter()
        ready = []
        self.controller.page_ready.connect(
            lambda _generation, snapshot, _image: ready.append(
                snapshot.page_number
            )
        )

        self.controller.update_viewport(
            "301",
            current_page=50,
            visible_pages=(50, 51),
            total_pages=100,
            target_width=800,
        )
        self.controller.update_viewport(
            "301",
            current_page=50,
            visible_pages=(50, 51),
            total_pages=100,
            target_width=800,
        )

        self.assertTrue(self.wait_until(lambda: 50 in ready and 51 in ready))
        self.assertLessEqual(self.controller.maximum_network_active, 3)
        self.assertLessEqual(
            self.controller.memory_cache_bytes,
            1024 * 1024,
        )
        self.assertLessEqual(self.controller.pending_page_count, 64)
        requested = [
            page
            for photo, page, _thread in self.service.calls
            if photo == "301" and page in {50, 51}
        ]
        self.assertEqual(requested.count(50), 1)
        self.assertEqual(requested.count(51), 1)
        main_thread = threading.get_ident()
        self.assertTrue(
            all(thread_id != main_thread for _, _, thread_id in self.service.calls)
        )

    def test_fast_scroll_drops_old_pending_and_generation_drops_late(self):
        catalog, _chapter = self.load_first_chapter()
        delivered = []
        self.controller.page_ready.connect(
            lambda generation, snapshot, _image: delivered.append(
                (generation, snapshot.photo_id, snapshot.page_number)
            )
        )
        old_generation = self.controller.generation
        self.controller.update_viewport(
            "301",
            current_page=5,
            visible_pages=(5,),
            total_pages=100,
            target_width=800,
        )
        self.controller.update_viewport(
            "301",
            current_page=90,
            visible_pages=(90,),
            total_pages=100,
            target_width=800,
        )
        new_generation = self.controller.load_chapter(
            catalog,
            "302",
            target_width=800,
        )

        self.assertGreater(new_generation, old_generation)
        self.assertTrue(
            self.wait_until(
                lambda: any(
                    generation == new_generation and photo == "302"
                    for generation, photo, _page in delivered
                )
            )
        )
        self.assertFalse(
            any(
                generation == old_generation
                for generation, _photo, _page in delivered
            )
        )

    def test_progress_is_debounced_and_leave_flushes_latest_page(self):
        _catalog, chapter = self.load_first_chapter()
        self.controller.set_history_context(
            album_id="123",
            title="测试漫画",
            chapter=chapter,
            source=ReaderSource.SEARCH,
        )
        for page in (3, 4, 9):
            self.controller.update_viewport(
                "301",
                current_page=page,
                visible_pages=(page,),
                total_pages=100,
                target_width=800,
            )
        self.assertTrue(
            self.wait_until(
                lambda: (
                    self.history.find("123") is not None
                    and self.history.find("123").page_number == 9
                )
            )
        )
        self.controller.update_viewport(
            "301",
            current_page=12,
            visible_pages=(12,),
            total_pages=100,
            target_width=800,
        )
        self.controller.leave()
        self.assertTrue(
            self.wait_until(
                lambda: self.history.find("123").page_number == 12
            )
        )

    def test_shutdown_is_bounded_and_closes_service(self):
        self.controller.open_album("123")

        self.assertTrue(self.controller.shutdown(2.0))
        self.assertTrue(self.service.closed)
        self.assertTrue(self.controller.worker_is_daemon)


if __name__ == "__main__":
    unittest.main()
