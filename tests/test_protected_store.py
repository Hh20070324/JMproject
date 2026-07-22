import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jm_downloader import protected_store
from jm_downloader.protected_store import (
    CurrentUserDpapi,
    ProtectedStore,
    ProtectedStoreDeleteError,
    ProtectedStoreKind,
    ProtectedStorePathError,
    ProtectedStoreUnreadableError,
    ProtectedStoreValidationError,
    ProtectedStoreWriteError,
    UnsupportedProtectedPayloadVersion,
    UnsupportedProtectedStoreVersion,
)
from jm_downloader.settings import AppPaths


class DeterministicProtector:
    PREFIX = b"phase-one-fake\0"

    def __init__(self):
        self.fail_protect = False
        self.fail_unprotect = False

    def protect(self, plaintext: bytes) -> bytes:
        if self.fail_protect:
            raise RuntimeError("secret backend failure")
        digest = hashlib.sha256(plaintext).digest()
        return self.PREFIX + digest + plaintext

    def unprotect(self, ciphertext: bytes) -> bytes:
        if self.fail_unprotect:
            raise RuntimeError("secret backend failure")
        prefix_size = len(self.PREFIX)
        digest_end = prefix_size + hashlib.sha256().digest_size
        if not ciphertext.startswith(self.PREFIX) or len(ciphertext) < digest_end:
            raise ValueError("invalid protected payload")
        digest = ciphertext[prefix_size:digest_end]
        plaintext = ciphertext[digest_end:]
        if digest != hashlib.sha256(plaintext).digest():
            raise ValueError("tampered protected payload")
        return plaintext


class ProtectedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(self.root)
        self.protector = DeterministicProtector()
        self.account = ProtectedStore.account(self.paths, self.protector)
        self.favorites = ProtectedStore.favorites(
            self.paths,
            self.protector,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _account_payload(**changes):
        payload = {
            "schema_version": 1,
            "uid": "10001",
            "username": "portable-user",
            "cookies": {"session": "private-cookie"},
        }
        payload.update(changes)
        return payload

    @staticmethod
    def _favorites_payload(**changes):
        payload = {
            "schema_version": 2,
            "uid": "10001",
            "folders": [
                {
                    "id": "0",
                    "name": "Default",
                    "items": [{"album_id": "1449491", "title": "Title"}],
                }
            ],
        }
        payload.update(changes)
        return payload

    def _write_envelope(
        self,
        store: ProtectedStore,
        ciphertext: bytes,
        *,
        version: int = 1,
        kind: str | None = None,
    ) -> bytes:
        raw = (
            json.dumps(
                {
                    "format": protected_store.ENVELOPE_FORMAT,
                    "schema_version": version,
                    "kind": kind or store.kind.value,
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        store.path.write_bytes(raw)
        return raw

    def test_factories_bind_only_to_fixed_portable_files(self):
        self.assertEqual(self.account.kind, ProtectedStoreKind.ACCOUNT)
        self.assertEqual(self.account.path, self.paths.account_file)
        self.assertEqual(self.favorites.kind, ProtectedStoreKind.FAVORITES)
        self.assertEqual(self.favorites.path, self.paths.favorites_file)
        self.assertEqual(self.account.payload_schema_version, 1)
        self.assertEqual(self.favorites.payload_schema_version, 2)
        self.assertGreater(
            self.favorites.max_plaintext_bytes,
            self.account.max_plaintext_bytes,
        )

    def test_missing_files_load_as_none_without_creating_state(self):
        self.assertIsNone(self.account.load())
        self.assertIsNone(self.favorites.load())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_account_and_favorites_round_trip_with_secret_payload_encrypted(self):
        account_payload = self._account_payload()
        favorites_payload = self._favorites_payload()

        self.account.save(account_payload)
        self.favorites.save(favorites_payload)

        self.assertEqual(self.account.load(), account_payload)
        self.assertEqual(self.favorites.load(), favorites_payload)
        account_raw = self.paths.account_file.read_bytes()
        favorites_raw = self.paths.favorites_file.read_bytes()
        for secret in (
            b"portable-user",
            b"private-cookie",
            b"1449491",
            b"Title",
            b"Default",
        ):
            self.assertNotIn(secret, account_raw)
            self.assertNotIn(secret, favorites_raw)

        outer = json.loads(account_raw.decode("ascii"))
        self.assertEqual(
            set(outer),
            {"format", "schema_version", "kind", "ciphertext"},
        )
        self.assertEqual(outer["kind"], "account")

    def test_store_kind_mismatch_is_rejected_without_rewriting_file(self):
        self.account.save(self._account_payload())
        raw = self.paths.account_file.read_bytes()
        self.paths.favorites_file.write_bytes(raw)

        with self.assertRaises(ProtectedStoreUnreadableError):
            self.favorites.load()

        self.assertEqual(self.paths.favorites_file.read_bytes(), raw)

    def test_cleanup_removes_only_ordinary_temporaries_for_current_store(self):
        account_temp = self.root / ".account.dat.interrupted.tmp"
        favorites_temp = self.root / ".favorites.dat.interrupted.tmp"
        unrelated = self.root / ".settings.json.interrupted.tmp"
        suspicious = self.root / ".account.dat.directory.tmp"
        account_temp.write_bytes(b"partial")
        favorites_temp.write_bytes(b"keep")
        unrelated.write_bytes(b"keep")
        suspicious.mkdir()

        self.account.cleanup_stale_temporaries()

        self.assertFalse(account_temp.exists())
        self.assertTrue(favorites_temp.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(suspicious.is_dir())

    def test_rejects_non_regular_target_without_touching_it(self):
        self.paths.account_file.mkdir()

        with self.assertRaises(ProtectedStorePathError):
            self.account.save(self._account_payload())

        self.assertTrue(self.paths.account_file.is_dir())

    def test_reparse_target_detection_fails_closed(self):
        self.paths.account_file.write_bytes(b"outside")
        with patch.object(
            protected_store,
            "_is_reparse_state",
            side_effect=[False, True],
        ):
            with self.assertRaises(ProtectedStorePathError):
                self.account.load()

        self.assertEqual(self.paths.account_file.read_bytes(), b"outside")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_rejects_portable_root_directory_junction(self):
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.root / "portable-link"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="ignore"))
        try:
            store = ProtectedStore.account(
                AppPaths(junction),
                self.protector,
            )
            with self.assertRaises(ProtectedStorePathError):
                store.save(self._account_payload())
        finally:
            os.rmdir(junction)

        self.assertEqual(list(outside.iterdir()), [])

    def test_future_envelope_is_refused_without_rewrite(self):
        raw = self._write_envelope(self.account, b"ciphertext", version=2)

        with self.assertRaises(UnsupportedProtectedStoreVersion):
            self.account.load()

        self.assertEqual(self.paths.account_file.read_bytes(), raw)

    def test_future_payload_is_refused_without_rewrite(self):
        plaintext = json.dumps({"schema_version": 2}).encode("utf-8")
        raw = self._write_envelope(
            self.account,
            self.protector.protect(plaintext),
        )

        with self.assertRaises(UnsupportedProtectedPayloadVersion):
            self.account.load()

        self.assertEqual(self.paths.account_file.read_bytes(), raw)

    def test_corrupt_truncated_duplicate_and_oversize_files_fail_closed(self):
        invalid_base64 = (
            b'{"ciphertext":"not base64!","format":'
            b'"jm-downloader.protected","kind":"account",'
            b'"schema_version":1}'
        )
        invalid_values = (
            b"not-json",
            b'{"format":"jm-downloader.protected"',
            (
                b'{"format":"jm-downloader.protected",'
                b'"format":"duplicate","schema_version":1,'
                b'"kind":"account","ciphertext":"eA=="}'
            ),
            invalid_base64,
            b"x" * (self.account.max_file_bytes + 1),
        )
        for raw in invalid_values:
            with self.subTest(size=len(raw)):
                self.paths.account_file.write_bytes(raw)
                with self.assertRaises(ProtectedStoreUnreadableError):
                    self.account.load()
                self.assertEqual(self.paths.account_file.read_bytes(), raw)

    def test_ciphertext_tampering_keeps_original_file(self):
        self.account.save(self._account_payload())
        envelope = json.loads(self.paths.account_file.read_text(encoding="ascii"))
        ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
        ciphertext[len(ciphertext) // 2] ^= 0x01
        raw = self._write_envelope(self.account, bytes(ciphertext))

        with self.assertRaises(ProtectedStoreUnreadableError):
            self.account.load()

        self.assertEqual(self.paths.account_file.read_bytes(), raw)

    def test_failed_protection_and_replace_preserve_old_valid_file(self):
        self.account.save(self._account_payload(username="old"))
        before = self.paths.account_file.read_bytes()

        self.protector.fail_protect = True
        with self.assertRaises(ProtectedStoreWriteError):
            self.account.save(self._account_payload(username="new"))
        self.protector.fail_protect = False
        self.assertEqual(self.paths.account_file.read_bytes(), before)

        with patch.object(
            protected_store.os,
            "replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(ProtectedStoreWriteError):
                self.account.save(self._account_payload(username="new"))

        self.assertEqual(self.paths.account_file.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".account.dat.*.tmp")), [])

    def test_temporary_creation_and_flush_failures_preserve_old_file(self):
        self.account.save(self._account_payload(username="old"))
        before = self.paths.account_file.read_bytes()

        with patch.object(
            protected_store.tempfile,
            "mkstemp",
            side_effect=OSError("creation failed"),
        ):
            with self.assertRaises(ProtectedStoreWriteError):
                self.account.save(self._account_payload(username="new"))
        self.assertEqual(self.paths.account_file.read_bytes(), before)

        with patch.object(
            protected_store.os,
            "fsync",
            side_effect=OSError("flush failed"),
        ):
            with self.assertRaises(ProtectedStoreWriteError):
                self.account.save(self._account_payload(username="new"))
        self.assertEqual(self.paths.account_file.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".account.dat.*.tmp")), [])

    def test_future_or_invalid_payload_never_overwrites_old_file(self):
        self.account.save(self._account_payload())
        before = self.paths.account_file.read_bytes()
        invalid_values = (
            [],
            {"schema_version": 2},
            {"schema_version": 1, "invalid": object()},
            {
                "schema_version": 1,
                "oversize": "x" * self.account.max_plaintext_bytes,
            },
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(
                    (
                        ProtectedStoreValidationError,
                        UnsupportedProtectedPayloadVersion,
                    )
                ):
                    self.account.save(value)
                self.assertEqual(self.paths.account_file.read_bytes(), before)

    def test_delete_removes_only_current_store_and_its_temporaries(self):
        self.account.save(self._account_payload())
        self.favorites.save(self._favorites_payload())
        account_temp = self.root / ".account.dat.pending.tmp"
        favorites_temp = self.root / ".favorites.dat.pending.tmp"
        account_temp.write_bytes(b"partial")
        favorites_temp.write_bytes(b"keep")

        self.account.delete()

        self.assertFalse(self.paths.account_file.exists())
        self.assertFalse(account_temp.exists())
        self.assertTrue(self.paths.favorites_file.is_file())
        self.assertTrue(favorites_temp.is_file())

    def test_delete_failure_does_not_claim_success(self):
        self.account.save(self._account_payload())
        with patch.object(Path, "unlink", side_effect=OSError("denied")):
            with self.assertRaises(ProtectedStoreDeleteError):
                self.account.delete()

        self.assertTrue(self.paths.account_file.is_file())

    def test_missing_or_replaced_root_is_never_created_or_followed(self):
        missing = self.root / "missing"
        store = ProtectedStore.account(AppPaths(missing), self.protector)
        with self.assertRaises(ProtectedStorePathError):
            store.save(self._account_payload())
        self.assertFalse(missing.exists())

    def test_gitignore_excludes_protected_runtime_files_and_temporaries(self):
        gitignore = (
            Path(__file__).resolve().parent.parent / ".gitignore"
        ).read_text(encoding="utf-8")
        for pattern in (
            "/account.dat",
            "/favorites.dat",
            "/.account.dat.*.tmp",
            "/.favorites.dat.*.tmp",
        ):
            self.assertIn(pattern, gitignore.splitlines())


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class CurrentUserDpapiStoreTests(unittest.TestCase):
    def test_real_dpapi_round_trip_and_tamper_failure_use_production_adapter(self):
        payload = {
            "schema_version": 1,
            "username": "real-dpapi-sentinel",
            "cookies": {"session": "real-cookie-sentinel"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            store = ProtectedStore.account(paths, CurrentUserDpapi())
            store.save(payload)
            raw = paths.account_file.read_bytes()

            self.assertNotIn(b"real-dpapi-sentinel", raw)
            self.assertNotIn(b"real-cookie-sentinel", raw)
            self.assertEqual(store.load(), payload)

            envelope = json.loads(raw.decode("ascii"))
            ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
            ciphertext[len(ciphertext) // 2] ^= 0x01
            envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
            tampered = (
                json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode("ascii")
            paths.account_file.write_bytes(tampered)

            with self.assertRaises(ProtectedStoreUnreadableError):
                store.load()
            self.assertEqual(paths.account_file.read_bytes(), tampered)

    def test_real_dpapi_store_survives_portable_directory_move(self):
        payload = {"schema_version": 1, "uid": "move-sentinel"}
        with tempfile.TemporaryDirectory() as temp_dir:
            first_root = Path(temp_dir) / "first"
            moved_root = Path(temp_dir) / "moved"
            first_root.mkdir()
            first_store = ProtectedStore.account(
                AppPaths(first_root),
                CurrentUserDpapi(),
            )
            first_store.save(payload)

            first_root.replace(moved_root)
            moved_store = ProtectedStore.account(
                AppPaths(moved_root),
                CurrentUserDpapi(),
            )

            self.assertEqual(moved_store.load(), payload)


if __name__ == "__main__":
    unittest.main()
