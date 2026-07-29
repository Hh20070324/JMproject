import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterSnapshot,
    ReaderContentMode,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
)
from jm_downloader.protected_store import ENVELOPE_FORMAT, ProtectedStore
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.reader import ReaderHistoryStore
from jm_downloader.settings import AppPaths


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
    "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DeterministicProtector:
    PREFIX = b"v32-history\0"

    def protect(self, plaintext):
        return self.PREFIX + hashlib.sha256(plaintext).digest() + plaintext

    def unprotect(self, ciphertext):
        prefix_end = len(self.PREFIX)
        digest_end = prefix_end + hashlib.sha256().digest_size
        plaintext = ciphertext[digest_end:]
        if (
            not ciphertext.startswith(self.PREFIX)
            or ciphertext[prefix_end:digest_end]
            != hashlib.sha256(plaintext).digest()
        ):
            raise ValueError
        return plaintext


class SourceService:
    def __init__(self, root: Path, name: str, delay=0.0):
        self.root = root
        self.name = name
        self.delay = delay
        self.calls = []
        self.closed = False

    async def fetch_catalog(self, album_id):
        self.calls.append(("catalog", str(album_id)))
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        return ChapterCatalogSnapshot(
            str(int(album_id)),
            f"{self.name}漫画",
            (ChapterSnapshot("301", 1, "第 1 章", downloaded=True),),
        )

    async def load_chapter(self, catalog, photo_id):
        self.calls.append(("chapter", photo_id))
        return (
            ReaderChapterSnapshot(photo_id, 1, "第 1 章", 1),
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


class ReaderHistoryV2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.protector = DeterministicProtector()
        self.protected = ProtectedStore.reading_history(
            self.paths,
            self.protector,
        )
        self.store = ReaderHistoryStore(
            self.protected,
            now=lambda: "2026-07-29T12:00:00Z",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write_v1(self):
        payload = {
            "schema_version": 1,
            "entries": [
                {
                    "album_id": "123",
                    "title": "旧历史",
                    "photo_id": "301",
                    "chapter_title": "第 1 章",
                    "chapter_index": 1,
                    "page_number": 2,
                    "page_count": 5,
                    "read_at_utc": "2026-07-28T12:00:00Z",
                    "source": "favorites",
                }
            ],
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        envelope = (
            json.dumps(
                {
                    "format": ENVELOPE_FORMAT,
                    "schema_version": 1,
                    "kind": "reading_history",
                    "ciphertext": base64.b64encode(
                        self.protector.protect(plaintext)
                    ).decode("ascii"),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        self.paths.reading_history_file.write_bytes(envelope)
        return envelope

    def test_v1_reads_as_online_without_rewriting_until_next_record(self):
        original = self._write_v1()

        entries = self.store.load()

        self.assertEqual(entries[0].content_mode, ReaderContentMode.ONLINE)
        self.assertEqual(self.paths.reading_history_file.read_bytes(), original)

        updated = self.store.record(
            album_id="456",
            title="本地历史",
            photo_id="601",
            chapter_title="第 1 章",
            chapter_index=1,
            page_number=1,
            page_count=3,
            source=ReaderSource.LOCAL_LIBRARY,
            content_mode=ReaderContentMode.LOCAL,
        )

        self.assertEqual(updated[0].content_mode, ReaderContentMode.LOCAL)
        payload = self.protected.load()
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["entries"][0]["content_mode"], "local")
        self.assertEqual(payload["entries"][1]["content_mode"], "online")


class ReaderSourceRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.online = SourceService(self.root, "online")
        self.local = SourceService(self.root, "local")
        self.controller = ReaderController(
            self.online,
            local_service=self.local,
            result_interval_ms=5,
        )

    def tearDown(self):
        self.controller.shutdown(2.0)
        self.temporary.cleanup()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_local_catalog_chapter_page_and_retry_never_call_online_service(self):
        catalogs = []
        chapters = []
        pages = []
        self.controller.catalog_ready.connect(
            lambda _generation, catalog: catalogs.append(catalog)
        )
        self.controller.chapter_ready.connect(
            lambda _generation, chapter, values: chapters.append(
                (chapter, values)
            )
        )
        self.controller.page_ready.connect(
            lambda _generation, snapshot, _image: pages.append(snapshot)
        )

        self.controller.open_album(
            "123",
            content_mode=ReaderContentMode.LOCAL,
        )
        self.assertTrue(self.wait_until(lambda: bool(catalogs)))
        self.controller.load_chapter(
            catalogs[0],
            "301",
            target_width=100,
        )
        self.assertTrue(self.wait_until(lambda: bool(chapters)))
        self.assertTrue(self.wait_until(lambda: bool(pages)))
        self.controller.retry_pages(
            "301",
            (1,),
            current_page=1,
            total_pages=1,
            target_width=100,
        )
        self.assertTrue(
            self.wait_until(
                lambda: sum(call[0] == "page" for call in self.local.calls)
                >= 2
            )
        )

        self.assertEqual(self.online.calls, [])
        self.assertEqual(self.controller.maximum_network_active, 0)

    def test_late_local_catalog_is_dropped_after_online_switch(self):
        self.controller.shutdown(2.0)
        self.local = SourceService(self.root, "local", delay=0.08)
        self.controller = ReaderController(
            self.online,
            local_service=self.local,
            result_interval_ms=5,
        )
        outcomes = []
        self.controller.catalog_ready.connect(
            lambda generation, catalog: outcomes.append(
                (generation, catalog.title)
            )
        )

        first = self.controller.open_album(
            "123",
            content_mode=ReaderContentMode.LOCAL,
        )
        second = self.controller.open_album(
            "456",
            content_mode=ReaderContentMode.ONLINE,
        )

        self.assertNotEqual(first, second)
        self.assertTrue(self.wait_until(lambda: bool(outcomes)))
        time.sleep(0.1)
        self.app.processEvents()
        self.assertEqual(outcomes, [(second, "online漫画")])

    def test_controller_persists_local_mode_in_shared_history(self):
        self.controller.shutdown(2.0)
        paths = AppPaths(self.root)
        history = ReaderHistoryStore(
            ProtectedStore.reading_history(paths, DeterministicProtector())
        )
        self.controller = ReaderController(
            self.online,
            local_service=self.local,
            history_store=history,
            result_interval_ms=5,
            history_debounce_ms=10,
        )
        self.controller.open_album(
            "123",
            content_mode=ReaderContentMode.LOCAL,
        )
        self.controller.set_history_context(
            album_id="123",
            title="本地漫画",
            chapter=ReaderChapterSnapshot("301", 1, "第 1 章", 5),
            source=ReaderSource.LOCAL_LIBRARY,
        )
        self.controller.update_viewport(
            "301",
            current_page=4,
            visible_pages=(4,),
            total_pages=5,
            target_width=100,
        )
        self.controller.flush_history()

        self.assertTrue(
            self.wait_until(lambda: history.find("123") is not None)
        )
        entry = history.find("123")
        self.assertEqual(entry.page_number, 4)
        self.assertEqual(entry.content_mode, ReaderContentMode.LOCAL)
        self.assertEqual(entry.source, ReaderSource.LOCAL_LIBRARY)


if __name__ == "__main__":
    unittest.main()
