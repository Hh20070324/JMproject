import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from jm_downloader.models import (
    ReaderErrorKind,
    ReaderHistoryEntry,
    ReaderPageSnapshot,
    ReaderPageState,
    ReaderSource,
)
from jm_downloader.protected_store import (
    ENVELOPE_FORMAT,
    ProtectedStore,
    ProtectedStoreKind,
    ProtectedStoreValidationError,
    UnsupportedProtectedPayloadVersion,
)
from jm_downloader.reader import (
    MAX_READING_HISTORY_ENTRIES,
    ReaderHistoryStore,
)
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    PREFIX = b"reader-history\0"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + hashlib.sha256(plaintext).digest() + plaintext

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix_end = len(self.PREFIX)
        digest_end = prefix_end + hashlib.sha256().digest_size
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid ciphertext")
        plaintext = ciphertext[digest_end:]
        if (
            ciphertext[prefix_end:digest_end]
            != hashlib.sha256(plaintext).digest()
        ):
            raise ValueError("invalid digest")
        return plaintext


class ReaderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temporary.name))
        self.protector = DeterministicProtector()
        self.protected = ProtectedStore.reading_history(
            self.paths,
            self.protector,
        )
        self.clock = 0

        def now():
            self.clock += 1
            return (
                datetime(2026, 7, 27, tzinfo=timezone.utc)
                + timedelta(seconds=self.clock)
            ).isoformat().replace("+00:00", "Z")

        self.store = ReaderHistoryStore(self.protected, now=now)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, album_id="123", **changes):
        numeric_album_id = int(
            str(album_id).strip().removeprefix("JM").removeprefix("jm")
        )
        values = {
            "album_id": album_id,
            "title": f"漫画 {album_id}",
            "photo_id": f"{numeric_album_id + 1000}",
            "chapter_title": "第 1 章",
            "chapter_index": 1,
            "page_number": 3,
            "page_count": 20,
            "source": ReaderSource.SEARCH,
        }
        values.update(changes)
        return self.store.record(**values)

    def test_models_are_frozen_and_use_stable_enums(self):
        page = ReaderPageSnapshot(
            "301",
            1,
            10,
            ReaderPageState.FAILED,
            error_kind=ReaderErrorKind.IMAGE_DAMAGED,
        )
        self.assertEqual(page.state.value, "failed")
        self.assertEqual(page.error_kind.value, "image_damaged")
        with self.assertRaises((AttributeError, TypeError)):
            page.page_number = 2

    def test_factory_uses_separate_portable_encrypted_file(self):
        self.assertEqual(
            self.protected.kind,
            ProtectedStoreKind.READING_HISTORY,
        )
        self.assertEqual(
            self.protected.path,
            self.paths.root / "reading_history.dat",
        )
        self.assertEqual(
            self.paths.reader_temp,
            self.paths.root / "ReaderTemp",
        )
        self.record()
        raw = self.paths.reading_history_file.read_bytes()
        self.assertNotIn(b"123", raw)
        self.assertNotIn("漫画".encode(), raw)

    def test_same_album_updates_and_moves_to_most_recent(self):
        self.record("123", page_number=2)
        self.record("456", page_number=4)
        self.record(
            "JM00123",
            page_number=9,
            source=ReaderSource.FAVORITES,
        )

        entries = self.store.load()

        self.assertEqual([entry.album_id for entry in entries], ["123", "456"])
        self.assertEqual(entries[0].page_number, 9)
        self.assertEqual(entries[0].source, ReaderSource.FAVORITES)
        self.assertEqual(self.store.find("00123"), entries[0])

    def test_recent_one_hundred_are_kept(self):
        for value in range(1, 112):
            self.record(str(value))

        entries = self.store.load()

        self.assertEqual(len(entries), MAX_READING_HISTORY_ENTRIES)
        self.assertEqual(entries[0].album_id, "111")
        self.assertEqual(entries[-1].album_id, "12")
        self.assertIsNone(self.store.find("1"))

    def test_remove_and_clear_do_not_touch_other_protected_files(self):
        self.paths.account_file.write_bytes(b"keep-account")
        self.record("123")
        self.record("456")

        remaining = self.store.remove("123")

        self.assertEqual([entry.album_id for entry in remaining], ["456"])
        self.store.clear()
        self.assertFalse(self.paths.reading_history_file.exists())
        self.assertEqual(self.paths.account_file.read_bytes(), b"keep-account")

    def test_invalid_page_and_unknown_fields_are_backed_up(self):
        self.record()
        payload = self.protected.load()
        payload["entries"][0]["page_number"] = 21
        payload["entries"][0]["unexpected"] = "no"
        self.protected.save(payload)

        self.assertEqual(self.store.load(), ())
        self.assertFalse(self.paths.reading_history_file.exists())
        self.assertIsNotNone(self.store.last_recovery_backup)

    def test_future_version_is_refused_without_rewrite(self):
        payload = json.dumps(
            {"schema_version": 2, "entries": []},
            separators=(",", ":"),
        ).encode()
        ciphertext = self.protector.protect(payload)
        envelope = (
            json.dumps(
                {
                    "format": ENVELOPE_FORMAT,
                    "schema_version": 1,
                    "kind": "reading_history",
                    "ciphertext": base64.b64encode(ciphertext).decode(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        self.paths.reading_history_file.write_bytes(envelope)

        with self.assertRaises(UnsupportedProtectedPayloadVersion):
            self.store.load()
        self.assertEqual(
            self.paths.reading_history_file.read_bytes(),
            envelope,
        )
        with self.assertRaises(UnsupportedProtectedPayloadVersion):
            self.record()
        self.assertEqual(
            self.paths.reading_history_file.read_bytes(),
            envelope,
        )

    def test_strict_validation_rejects_false_integers_and_urls(self):
        with self.assertRaises(ProtectedStoreValidationError):
            self.record(page_number=True)
        with self.assertRaises(ProtectedStoreValidationError):
            self.record(title="https://cdn.invalid/" + "x" * 500)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class ReaderHistoryDpapiTests(unittest.TestCase):
    def test_current_user_dpapi_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            store = ReaderHistoryStore(
                ProtectedStore.reading_history(paths)
            )
            store.record(
                album_id="123",
                title="DPAPI 漫画",
                photo_id="301",
                chapter_title="第一章",
                chapter_index=1,
                page_number=7,
                page_count=20,
                source=ReaderSource.EXACT_SEARCH,
            )

            self.assertEqual(store.load()[0].page_number, 7)
            self.assertNotIn(
                "DPAPI 漫画".encode("utf-8"),
                paths.reading_history_file.read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
