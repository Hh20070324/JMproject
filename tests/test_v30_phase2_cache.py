from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from jm_downloader.reader import (
    ReaderCacheExhausted,
    ReaderDiskCache,
    ReaderMemoryCache,
    ReaderTempError,
)


class IncrementingClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return float(self.value)


class ReaderMemoryCacheTests(unittest.TestCase):
    def test_farthest_then_oldest_unpinned_page_is_evicted(self):
        cache = ReaderMemoryCache[str](10, clock=IncrementingClock())
        cache.put(
            "page-1",
            "one",
            byte_size=3,
            page_number=1,
            current_page=1,
        )
        cache.put(
            "page-5",
            "five",
            byte_size=3,
            page_number=5,
            current_page=5,
        )
        cache.put(
            "page-9",
            "nine",
            byte_size=3,
            page_number=9,
            current_page=9,
        )

        evicted = cache.put(
            "page-6",
            "six",
            byte_size=4,
            page_number=6,
            current_page=6,
            pinned_keys={"page-5"},
        )

        self.assertEqual(evicted, ("page-1",))
        self.assertEqual(cache.total_bytes, 10)
        self.assertEqual(
            set(cache.keys()),
            {"page-5", "page-6", "page-9"},
        )

    def test_visible_pages_never_evicted_and_exhaustion_is_explicit(self):
        cache = ReaderMemoryCache[bytes](5)
        cache.put(
            "visible",
            b"12345",
            byte_size=5,
            page_number=1,
            current_page=1,
            pinned_keys={"visible"},
        )

        with self.assertRaises(ReaderCacheExhausted):
            cache.put(
                "next",
                b"x",
                byte_size=1,
                page_number=2,
                current_page=2,
                pinned_keys={"visible", "next"},
            )

        self.assertEqual(cache.get("visible"), b"12345")
        self.assertIsNone(cache.get("next"))
        self.assertEqual(cache.total_bytes, 5)


class ReaderDiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.portable_root = Path(self.temporary.name)
        self.reader_temp = self.portable_root / "ReaderTemp"

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, cache, page, content, **kwargs):
        reservation = cache.reserve(page, ".jpg")
        reservation.part_path.write_bytes(content)
        key, evicted = cache.publish(
            reservation,
            current_page=kwargs.pop("current_page", page),
            pinned_keys=kwargs.pop("pinned_keys", ()),
        )
        self.assertEqual(kwargs, {})
        return key, evicted, reservation.final_path

    def test_random_names_atomic_publish_lru_and_clean_close(self):
        cache = ReaderDiskCache(
            self.reader_temp,
            budget_bytes=10,
            clock=IncrementingClock(),
        )
        first, _, first_path = self.publish(cache, 1, b"111")
        middle, _, middle_path = self.publish(cache, 5, b"555")
        last, _, last_path = self.publish(cache, 9, b"999")

        current, evicted, current_path = self.publish(
            cache,
            6,
            b"6666",
            current_page=6,
            pinned_keys={middle},
        )

        self.assertEqual(evicted, (first,))
        self.assertFalse(first_path.exists())
        self.assertTrue(middle_path.exists())
        self.assertTrue(last_path.exists())
        self.assertTrue(current_path.exists())
        self.assertEqual(cache.total_bytes, 10)
        for path in (middle_path, last_path, current_path):
            self.assertNotIn("title", path.name)
            self.assertRegex(path.name, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(cache.path_for(current), current_path)
        session = cache.session_dir
        self.assertTrue(cache.close())
        self.assertFalse(session.exists())

    def test_pinned_budget_exhaustion_removes_part_not_visible_page(self):
        cache = ReaderDiskCache(self.reader_temp, budget_bytes=5)
        visible, _, visible_path = self.publish(cache, 1, b"12345")
        reservation = cache.reserve(2, ".png")
        reservation.part_path.write_bytes(b"x")

        with self.assertRaises(ReaderCacheExhausted):
            cache.publish(
                reservation,
                current_page=2,
                pinned_keys={visible, reservation.key},
            )

        self.assertTrue(visible_path.exists())
        self.assertFalse(reservation.part_path.exists())
        self.assertEqual(cache.total_bytes, 5)

    def test_tampered_reservation_and_linked_part_are_rejected(self):
        cache = ReaderDiskCache(self.reader_temp, budget_bytes=20)
        reservation = cache.reserve(1, ".jpg")
        reservation.part_path.write_bytes(b"valid")
        outside = self.portable_root / "outside.jpg"
        outside.write_bytes(b"outside")

        with self.assertRaises(ReaderTempError):
            cache.publish(
                replace(reservation, final_path=outside),
                current_page=1,
            )
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_startup_removes_only_safe_stale_sessions(self):
        self.reader_temp.mkdir()
        stale = self.reader_temp / ("session-" + "a" * 32)
        stale.mkdir()
        (stale / "page.jpg").write_bytes(b"old")
        unrelated = self.reader_temp / "user-file.txt"
        unrelated.write_bytes(b"keep")
        outside = self.portable_root / "outside.txt"
        outside.write_bytes(b"outside")

        cache = ReaderDiskCache(self.reader_temp, budget_bytes=20)

        self.assertFalse(stale.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(outside.read_bytes(), b"outside")
        cache.close()

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_stale_session_with_junction_is_left_untouched(self):
        self.reader_temp.mkdir()
        stale = self.reader_temp / ("session-" + "b" * 32)
        stale.mkdir()
        outside = self.portable_root / "outside"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_bytes(b"keep")
        junction = stale / "escape"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前环境不能创建目录联接")
        try:
            cache = ReaderDiskCache(self.reader_temp, budget_bytes=20)
            self.assertEqual(cache.cleanup_failures, (stale,))
            self.assertTrue(stale.exists())
            self.assertEqual(marker.read_bytes(), b"keep")
            cache.close()
        finally:
            if junction.exists():
                os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_reader_temp_root_junction_is_rejected(self):
        outside = self.portable_root / "outside"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_bytes(b"keep")
        result = subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                str(self.reader_temp),
                str(outside),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前环境不能创建目录联接")
        try:
            with self.assertRaises(ReaderTempError):
                ReaderDiskCache(self.reader_temp, budget_bytes=20)
            self.assertEqual(marker.read_bytes(), b"keep")
        finally:
            if self.reader_temp.exists():
                os.rmdir(self.reader_temp)


if __name__ == "__main__":
    unittest.main()
