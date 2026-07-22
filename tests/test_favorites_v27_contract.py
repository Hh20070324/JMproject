from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import jm_downloader.favorites as favorites_module
from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import (
    FavoriteCacheStore,
    FavoritesCache,
    FavoritesLocalDataError,
    FavoritesService,
)
from jm_downloader.models import (
    FavoriteFolderSnapshot,
    FavoriteItemSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 22, 8, 30, tzinfo=timezone.utc)


class ContractProtector:
    PREFIX = b"favorites-v27-contract\0"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid ciphertext")
        return ciphertext[len(self.PREFIX) :][::-1]


class FavoritesV27ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.protector = ContractProtector()
        self.account_protected = ProtectedStore.account(
            self.paths,
            self.protector,
        )
        self.favorites_protected = ProtectedStore.favorites(
            self.paths,
            self.protector,
        )
        self.account_service = AccountService(
            self.paths,
            account_store=AccountStore(self.account_protected),
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        self.account_service.login(
            "test-user",
            "test-password",
            self.account_service.start_operation(),
        )
        self.cache_store = FavoriteCacheStore(self.favorites_protected)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def folders(*, custom_items=()):
        return {
            "0": (
                "Default",
                (
                    ("1", {"name": "One", "author": "Author A"}),
                    ("2", {"name": "Two"}),
                ),
            ),
            "8": ("Reading", tuple(custom_items)),
        }

    def service(self, client):
        factory_calls = []

        def factory(cookies):
            factory_calls.append(dict(cookies))
            return client

        service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=self.cache_store,
            client_factory=factory,
            clock=lambda: FIXED_TIME,
        )
        service.factory_calls = factory_calls
        return service

    def write_legacy_payload(self, payload):
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        ciphertext = self.protector.protect(plaintext)
        envelope = self.favorites_protected._encode_envelope(ciphertext)
        self.paths.favorites_file.write_bytes(envelope)

    @staticmethod
    def schema_v1_payload():
        return {
            "schema_version": 1,
            "account_uid": "10001",
            "synced_at_utc": "2026-07-21T12:00:00Z",
            "folders": [
                {
                    "folder_id": "0",
                    "name": "全部收藏",
                    "items": [
                        {
                            "album_id": "1",
                            "title": "Old item",
                            "authors": [],
                            "tags": [],
                        }
                    ],
                }
            ],
        }

    def test_schema_v2_round_trip_preserves_mr_or_mp_order(self):
        cache = FavoritesCache(
            "10001",
            "2026-07-22T08:30:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "全部收藏",
                    (FavoriteItemSnapshot("1", "One"),),
                ),
            ),
            order_by="mp",
        )

        payload = cache.to_payload()
        restored = FavoritesCache.from_payload(payload)

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["order_by"], "mp")
        self.assertEqual(restored, cache)
        self.assertEqual(restored.to_snapshot().order_by, "mp")

    def test_schema_v1_restores_as_mr_without_rewriting_ciphertext(self):
        self.write_legacy_payload(self.schema_v1_payload())
        before = self.paths.favorites_file.read_bytes()
        service = self.service(FakeJmAccountClient())

        snapshot = service.restore(service.start_operation())

        self.assertEqual(snapshot.order_by, "mr")
        self.assertEqual(self.paths.favorites_file.read_bytes(), before)
        self.assertEqual(service.factory_calls, [])

    def test_future_schema_is_refused_without_rewriting_ciphertext(self):
        payload = self.schema_v1_payload()
        payload["schema_version"] = 3
        payload["order_by"] = "mr"
        self.write_legacy_payload(payload)
        before = self.paths.favorites_file.read_bytes()

        with self.assertRaises(FavoritesLocalDataError):
            self.cache_store.load("10001")

        self.assertEqual(self.paths.favorites_file.read_bytes(), before)

    def test_folder_zero_is_always_presented_as_all_favorites(self):
        client = FakeJmAccountClient(folders=self.folders())
        service = self.service(client)

        snapshot = service.sync(service.start_operation())

        self.assertEqual(snapshot.folders[0].folder_id, "0")
        self.assertEqual(snapshot.folders[0].name, "全部收藏")

    def test_mp_sync_uses_one_order_for_every_page_and_persists_it(self):
        client = FakeJmAccountClient(
            folders=self.folders(custom_items=(("2", {"name": "Two"}),)),
            page_size=1,
        )
        service = self.service(client)

        snapshot = service.sync(service.start_operation(), order_by="mp")

        page_calls = [
            call for call in client.calls if call[0] == "favorite_folder"
        ]
        self.assertTrue(page_calls)
        self.assertTrue(all(call[2] == "mp" for call in page_calls))
        self.assertEqual(snapshot.order_by, "mp")
        self.assertEqual(self.cache_store.load("10001").order_by, "mp")

    def test_unknown_sync_order_is_rejected_before_client_creation(self):
        client = FakeJmAccountClient(folders=self.folders())
        service = self.service(client)

        with self.assertRaises(ValueError):
            service.sync(service.start_operation(), order_by="newest")

        self.assertEqual(service.factory_calls, [])
        self.assertEqual(client.calls, [])

    def test_create_folder_trims_name_and_disables_write_retries(self):
        client = FakeJmAccountClient(folders=self.folders())
        client.retry_times = 4
        service = self.service(client)
        service.sync(service.start_operation())
        client.calls.clear()

        folder_name = service.create_folder(
            "  Later  ",
            service.start_operation(),
        )

        self.assertEqual(folder_name, "Later")
        self.assertEqual(client.retry_times, 0)
        self.assertEqual(
            client.calls,
            [
                (
                    "favorite_folder_mutation",
                    "add",
                    {"type": "add", "folder_name": "Later"},
                )
            ],
        )

    def test_nonempty_folder_is_checked_online_and_never_deleted(self):
        client = FakeJmAccountClient(
            folders=self.folders(custom_items=(("2", {"name": "Two"}),))
        )
        service = self.service(client)
        service.sync(service.start_operation())
        client.calls.clear()
        error_type = getattr(favorites_module, "FavoritesFolderNotEmpty")

        with self.assertRaises(error_type):
            service.delete_folder("8", service.start_operation())

        self.assertEqual(
            client.calls,
            [("favorite_folder", 1, "mr", "8", "")],
        )

    def test_empty_folder_delete_preflights_then_posts_once_without_retry(self):
        client = FakeJmAccountClient(folders=self.folders())
        client.retry_times = 5
        service = self.service(client)
        service.sync(service.start_operation())
        client.calls.clear()

        deleted_id = service.delete_folder("8", service.start_operation())

        self.assertEqual(deleted_id, "8")
        self.assertEqual(client.retry_times, 0)
        self.assertEqual(
            client.calls,
            [
                ("favorite_folder", 1, "mr", "8", ""),
                (
                    "favorite_folder_mutation",
                    "del",
                    {"type": "del", "folder_id": "8"},
                ),
            ],
        )

    def test_move_accepts_one_known_target_and_uses_empty_id_for_default(self):
        client = FakeJmAccountClient(
            folders=self.folders(custom_items=(("2", {"name": "Two"}),))
        )
        service = self.service(client)
        service.sync(service.start_operation())
        client.calls.clear()

        service.move_album("1", "8", service.start_operation())
        service.move_album("2", "0", service.start_operation())

        self.assertEqual(
            client.calls,
            [
                (
                    "favorite_folder_mutation",
                    "move",
                    {"type": "move", "aid": "1", "folder_id": "8"},
                ),
                (
                    "favorite_folder_mutation",
                    "move",
                    {"type": "move", "aid": "2", "folder_id": ""},
                ),
            ],
        )

    def test_move_rejects_multiple_or_unknown_targets_before_post(self):
        client = FakeJmAccountClient(folders=self.folders())
        service = self.service(client)
        service.sync(service.start_operation())
        client.calls.clear()

        for target in (("8", "9"), ["8"], "99"):
            with self.subTest(target=target):
                with self.assertRaises((TypeError, ValueError)):
                    service.move_album("1", target, service.start_operation())

        self.assertEqual(client.calls, [])

    def test_uncertain_folder_write_is_submitted_once_and_keeps_snapshot(self):
        client = FakeJmAccountClient(folders=self.folders())
        service = self.service(client)
        old_snapshot = service.sync(service.start_operation())
        client.calls.clear()
        client.retry_times = 7
        client.favorite_folder_mutation_errors["add"] = TimeoutError(
            "private endpoint"
        )
        error_type = getattr(favorites_module, "FavoritesMutationUncertain")

        with self.assertRaises(error_type):
            service.create_folder("Later", service.start_operation())

        self.assertEqual(client.retry_times, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0][0:2],
            ("favorite_folder_mutation", "add"),
        )
        self.assertIs(service.snapshot, old_snapshot)


if __name__ == "__main__":
    unittest.main()
