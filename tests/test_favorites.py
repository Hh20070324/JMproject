from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import logging
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jmcomic

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import (
    FavoriteCacheStore,
    FavoritesAccountMismatch,
    FavoritesAddUncertain,
    FavoritesCache,
    FavoritesInvalidAlbumId,
    FavoritesLocalDataError,
    FavoritesOperationCancelled,
    FavoritesResponseError,
    FavoritesService,
    FavoritesSessionExpired,
    FavoritesSessionRequired,
    FavoritesStorageError,
    FavoritesUnavailable,
)
from jm_downloader.models import (
    AccountStatus,
    FavoriteFolderSnapshot,
    FavoriteItemSnapshot,
)
from jm_downloader.protected_store import (
    ProtectedStore,
    ProtectedStoreWriteError,
)
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeFavoritePage, FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 16, 12, 45, tzinfo=timezone.utc)


class TestProtector:
    PREFIX = b"favorites-test\0"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid ciphertext")
        return ciphertext[len(self.PREFIX) :][::-1]


class FavoritesServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(self.root)
        self.protector = TestProtector()
        self.account_protected = ProtectedStore.account(
            self.paths,
            self.protector,
        )
        self.favorites_protected = ProtectedStore.favorites(
            self.paths,
            self.protector,
        )
        self.account_store = AccountStore(self.account_protected)
        self.cache_store = FavoriteCacheStore(self.favorites_protected)
        self.account_client = FakeJmAccountClient()
        self.account_service = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: self.account_client,
            clock=lambda: FIXED_TIME,
        )
        operation = self.account_service.start_operation()
        self.account_service.login(
            "test-user",
            "test-password",
            operation,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def populated_folders():
        return {
            "0": (
                "Default",
                (
                    (
                        "1449491",
                        {
                            "name": "First favorite",
                            "author": "Author A",
                            "tags": ["Tag A", "Tag B"],
                        },
                    ),
                    ("350234", {"name": "Second favorite"}),
                    ("3", {"name": "Third favorite"}),
                ),
            ),
            "8": (
                "Second folder",
                (("4", {"name": "Folder item", "authors": ["B"]}),),
            ),
        }

    def service(self, client):
        calls = []

        def factory(cookies):
            calls.append(dict(cookies))
            return client

        service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=self.cache_store,
            client_factory=factory,
            clock=lambda: FIXED_TIME,
        )
        service.factory_calls = calls
        return service

    def sync(self, service, progress_callback=None):
        operation = service.start_operation()
        return service.sync(operation, progress_callback)

    def add(self, service, album_id="777777"):
        operation = service.start_operation()
        return service.add_album(album_id, operation)

    def test_complete_sync_reads_default_custom_and_all_pages(self):
        client = FakeJmAccountClient(
            folders=self.populated_folders(),
            page_size=2,
        )
        service = self.service(client)
        progress = []

        snapshot = self.sync(service, progress.append)

        self.assertEqual(snapshot.synced_at_utc, "2026-07-16T12:45:00Z")
        self.assertEqual(
            [(folder.folder_id, folder.name) for folder in snapshot.folders],
            [("0", "Default"), ("8", "Second folder")],
        )
        self.assertEqual(
            [item.album_id for item in snapshot.folders[0].items],
            ["1449491", "350234", "3"],
        )
        first = snapshot.folders[0].items[0]
        self.assertEqual(first.authors, ("Author A",))
        self.assertEqual(first.tags, ("Tag A", "Tag B"))
        self.assertFalse(hasattr(snapshot, "account_uid"))
        self.assertFalse(hasattr(first, "cover_url"))
        self.assertEqual(
            client.calls,
            [
                ("favorite_folder", 1, "mr", "0", ""),
                ("favorite_folder", 2, "mr", "0", ""),
                ("favorite_folder", 1, "mr", "8", ""),
            ],
        )
        self.assertEqual([item.page for item in progress], [1, 2, 1])
        self.assertEqual(service.factory_calls[0]["AVS"], "test-avs")
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.SIGNED_IN,
        )
        raw = self.paths.favorites_file.read_bytes()
        for secret in (
            b"First favorite",
            b"Author A",
            b"Second folder",
            b"1449491",
        ):
            self.assertNotIn(secret, raw)

    def test_add_normalizes_id_disables_retries_and_does_not_touch_cache(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()
        client = FakeJmAccountClient(folders=self.populated_folders())
        client.retry_times = 3
        service = self.service(client)

        result = self.add(service, " JM 0777777 ")

        self.assertEqual(result, "777777")
        self.assertEqual(client.retry_times, 0)
        self.assertEqual(
            client.calls,
            [("add_favorite_album", "777777", "0")],
        )
        self.assertEqual(
            service.factory_calls,
            [{"session": "test-cookie", "AVS": "test-avs"}],
        )
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)
        self.assertIsNone(service.snapshot)

    def test_add_service_dispatches_only_the_post_workaround(self):
        response = object()

        class PostOnlyClient:
            API_FAVORITE = "/favorite"
            retry_times = 3
            domain_retry_strategy = None

            def __init__(self):
                self.calls = []

            def req_api(self, *args, **kwargs):
                self.calls.append(("req_api", args, kwargs))
                return response

            def require_resp_status_ok(self, value):
                self.calls.append(("require_resp_status_ok", value))

        client = PostOnlyClient()
        service = self.service(client)

        self.assertEqual(self.add(service), "777777")

        self.assertEqual(client.retry_times, 0)
        self.assertEqual(
            client.calls,
            [
                (
                    "req_api",
                    ("/favorite",),
                    {"get": False, "data": {"aid": "777777"}},
                ),
                ("require_resp_status_ok", response),
            ],
        )

    def test_add_refuses_domain_retry_strategy_before_remote_write(self):
        client = FakeJmAccountClient(folders=self.populated_folders())
        client.domain_retry_strategy = object()
        service = self.service(client)

        with self.assertRaises(FavoritesResponseError) as raised:
            self.add(service)

        self.assertEqual(str(raised.exception), "收藏客户端不支持安全写入")
        self.assertEqual(client.calls, [])
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_never_reads_or_writes_the_favorites_cache(self):
        class RejectingCacheStore:
            def __getattribute__(self, name):
                if name.startswith("__"):
                    return object.__getattribute__(self, name)
                raise AssertionError(f"cache access is forbidden: {name}")

        client = FakeJmAccountClient(folders=self.populated_folders())
        service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=RejectingCacheStore(),
            client_factory=lambda _cookies: client,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(self.add(service), "777777")
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_rejects_invalid_ids_before_client_creation(self):
        client = FakeJmAccountClient()
        service = self.service(client)

        for value in ("", "JM ", "12A", "1" * 33, None):
            with self.subTest(value=value):
                with self.assertRaises(FavoritesInvalidAlbumId):
                    self.add(service, value)

        self.assertEqual(service.factory_calls, [])
        self.assertEqual(client.calls, [])
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_requires_a_current_session(self):
        self.account_service.logout(self.account_service.prepare_logout())
        client = FakeJmAccountClient()
        service = self.service(client)

        with self.assertRaises(FavoritesSessionRequired):
            self.add(service)

        self.assertEqual(service.factory_calls, [])
        self.assertEqual(client.calls, [])

    def test_add_confirms_a_restored_session_without_writing_cache(self):
        restored_account = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: self.fail("restore went online"),
            clock=lambda: FIXED_TIME,
        )
        restored_account.restore(restored_account.start_operation())
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = FavoritesService(
            restored_account,
            self.paths,
            cache_store=self.cache_store,
            client_factory=lambda _cookies: client,
            clock=lambda: FIXED_TIME,
        )

        service.add_album("777777", service.start_operation())

        self.assertEqual(
            restored_account.snapshot.status,
            AccountStatus.SIGNED_IN,
        )
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_network_failure_is_uncertain_safe_and_not_retried(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()
        client = FakeJmAccountClient(folders=self.populated_folders())
        client.retry_times = 5
        secret = "private-response-and-endpoint"
        client.favorite_add_error = TimeoutError(secret)
        service = self.service(client)

        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            with self.assertRaises(FavoritesAddUncertain) as raised:
                self.add(service)

        self.assertEqual(client.retry_times, 0)
        self.assertEqual(
            client.calls,
            [("add_favorite_album", "777777", "0")],
        )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.SIGNED_IN,
        )

    def test_add_expired_session_is_marked_without_touching_cache(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()
        client = FakeJmAccountClient(folders=self.populated_folders())
        client.favorite_add_error = PermissionError("private response")
        service = self.service(client)

        with self.assertRaises(FavoritesSessionExpired):
            self.add(service)

        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.EXPIRED,
        )
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)

    def test_add_discards_a_result_after_the_operation_is_cancelled(self):
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = self.service(client)
        original = client.add_favorite_album

        def cancel_after_remote_write(*args, **kwargs):
            result = original(*args, **kwargs)
            service.cancel_operations()
            return result

        client.add_favorite_album = cancel_after_remote_write

        with self.assertRaises(FavoritesOperationCancelled):
            self.add(service)

        self.assertEqual(
            client.calls,
            [("add_favorite_album", "777777", "0")],
        )
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_transport_and_unknown_response_failures_are_uncertain_once(self):
        failures = (
            ConnectionResetError("private-connection-endpoint"),
            jmcomic.ResponseUnexpectedException(
                "private-response-body",
                {"resp": SimpleNamespace(http_code=500)},
            ),
            RuntimeError("private-unknown-upstream-state"),
        )
        for error in failures:
            with self.subTest(error_type=type(error).__name__):
                client = FakeJmAccountClient(
                    folders=self.populated_folders()
                )
                client.retry_times = 7
                client.favorite_add_error = error
                service = self.service(client)

                with self.assertLogs("jm-downloader", logging.WARNING) as logs:
                    with self.assertRaises(FavoritesAddUncertain) as raised:
                        self.add(service)

                output = "\n".join(logs.output)
                self.assertEqual(client.retry_times, 0)
                self.assertEqual(
                    client.calls,
                    [("add_favorite_album", "777777", "0")],
                )
                self.assertNotIn(str(error), str(raised.exception))
                self.assertNotIn(str(error), output)
                self.assertFalse(self.paths.favorites_file.exists())

    def test_add_response_403_expires_session_without_exposing_response(self):
        secret = "private-forbidden-response"
        client = FakeJmAccountClient(folders=self.populated_folders())
        client.favorite_add_error = jmcomic.ResponseUnexpectedException(
            secret,
            {"resp": SimpleNamespace(status_code=403)},
        )
        service = self.service(client)

        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            with self.assertRaises(FavoritesSessionExpired) as raised:
                self.add(service)

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.EXPIRED,
        )
        self.assertEqual(
            client.calls,
            [("add_favorite_album", "777777", "0")],
        )

    def test_add_refuses_a_client_that_cannot_disable_retries(self):
        class RetryLockedClient(FakeJmAccountClient):
            @property
            def retry_times(self):
                return 3

            @retry_times.setter
            def retry_times(self, _value):
                pass

        client = RetryLockedClient(folders=self.populated_folders())
        service = self.service(client)

        with self.assertRaises(FavoritesResponseError) as raised:
            self.add(service)

        self.assertEqual(str(raised.exception), "收藏客户端不支持安全写入")
        self.assertEqual(client.calls, [])
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_preserves_the_loaded_snapshot_and_last_sync_timestamp(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = self.service(client)
        restored = service.restore(service.start_operation())
        before = self.paths.favorites_file.read_bytes()

        result = self.add(service)

        self.assertEqual(result, "777777")
        self.assertIs(service.snapshot, restored)
        self.assertEqual(
            service.snapshot.synced_at_utc,
            "2026-07-16T12:00:00Z",
        )
        self.assertEqual(self.paths.favorites_file.read_bytes(), before)
        self.assertEqual(
            client.calls,
            [("add_favorite_album", "777777", "0")],
        )

    def test_restore_is_offline_and_empty_cache_does_not_create_file(self):
        service = self.service(FakeJmAccountClient())

        snapshot = service.restore(service.start_operation())

        self.assertIsNone(snapshot.synced_at_utc)
        self.assertEqual(snapshot.folders, ())
        self.assertEqual(service.factory_calls, [])
        self.assertFalse(self.paths.favorites_file.exists())

    def test_empty_default_folder_is_a_successful_complete_snapshot(self):
        service = self.service(
            FakeJmAccountClient(folders={"0": ("Default", ())})
        )

        snapshot = self.sync(service)

        self.assertEqual(len(snapshot.folders), 1)
        self.assertEqual(snapshot.folders[0].name, "Default")
        self.assertEqual(snapshot.folders[0].items, ())
        self.assertTrue(self.paths.favorites_file.is_file())

    def test_network_failure_keeps_old_cache_and_account_session(self):
        initial = self.service(
            FakeJmAccountClient(folders=self.populated_folders())
        )
        old_snapshot = self.sync(initial)
        old_bytes = self.paths.favorites_file.read_bytes()
        client = FakeJmAccountClient(
            folders=self.populated_folders(),
            page_size=2,
        )
        client.favorite_errors[("0", 2)] = TimeoutError("secret endpoint")
        service = self.service(client)
        service.restore(service.start_operation())

        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            with self.assertRaises(FavoritesUnavailable) as raised:
                self.sync(service)

        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)
        self.assertEqual(service.snapshot, old_snapshot)
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.SIGNED_IN,
        )

    def test_expired_session_keeps_cache_available_for_offline_restore(self):
        initial = self.service(
            FakeJmAccountClient(folders=self.populated_folders())
        )
        old_snapshot = self.sync(initial)
        old_bytes = self.paths.favorites_file.read_bytes()
        expired_client = FakeJmAccountClient(
            folders=self.populated_folders()
        )
        expired_client.favorite_errors[("0", 1)] = PermissionError(
            "private response"
        )
        service = self.service(expired_client)

        with self.assertRaises(FavoritesSessionExpired):
            self.sync(service)

        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.EXPIRED,
        )
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)
        restored = service.restore(service.start_operation())
        self.assertEqual(restored, old_snapshot)
        with self.assertRaises(FavoritesSessionRequired):
            self.sync(service)

    def test_saved_session_becomes_signed_in_after_successful_sync(self):
        restored_account = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: self.fail("restore went online"),
            clock=lambda: FIXED_TIME,
        )
        restored_account.restore(restored_account.start_operation())
        self.assertEqual(
            restored_account.snapshot.status,
            AccountStatus.SAVED_SESSION,
        )
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = FavoritesService(
            restored_account,
            self.paths,
            cache_store=self.cache_store,
            client_factory=lambda _cookies: client,
            clock=lambda: FIXED_TIME,
        )

        service.sync(service.start_operation())

        self.assertEqual(
            restored_account.snapshot.status,
            AccountStatus.SIGNED_IN,
        )

    def test_cooperative_cancel_stops_before_next_page_and_keeps_cache(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()
        client = FakeJmAccountClient(
            folders=self.populated_folders(),
            page_size=2,
        )
        service = self.service(client)

        def cancel_after_first_page(_progress):
            service.cancel_operations()

        with self.assertRaises(FavoritesOperationCancelled):
            self.sync(service, cancel_after_first_page)

        self.assertEqual(
            client.calls,
            [("favorite_folder", 1, "mr", "0", "")],
        )
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)

    def test_large_favorite_folder_syncs_all_pages_without_truncation(self):
        items = tuple(
            (
                str(index),
                {
                    "name": f"Favorite {index}",
                    "author": f"Author {index % 7}",
                    "tags": [f"Tag {index % 11}"],
                },
            )
            for index in range(1, 1_206)
        )
        client = FakeJmAccountClient(
            folders={"0": ("Default", items)},
            page_size=37,
        )
        service = self.service(client)

        snapshot = self.sync(service)

        self.assertEqual(len(snapshot.folders), 1)
        self.assertEqual(len(snapshot.folders[0].items), 1_205)
        self.assertEqual(snapshot.folders[0].items[0].album_id, "1")
        self.assertEqual(snapshot.folders[0].items[-1].album_id, "1205")
        self.assertEqual(
            len(
                [
                    call
                    for call in client.calls
                    if call[0] == "favorite_folder"
                ]
            ),
            33,
        )

    def test_storage_failure_preserves_old_cache_and_snapshot(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()
        service = self.service(
            FakeJmAccountClient(folders=self.populated_folders())
        )
        old_snapshot = service.restore(service.start_operation())

        with patch.object(
            self.favorites_protected,
            "_write_atomic",
            side_effect=ProtectedStoreWriteError("private disk details"),
        ):
            with self.assertRaises(FavoritesStorageError) as raised:
                self.sync(service)

        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_bytes)
        self.assertEqual(service.snapshot, old_snapshot)
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.SIGNED_IN,
        )

    def test_cache_decryption_failure_preserves_original_ciphertext(self):
        self.cache_store.save(self._sample_cache())
        original = self.paths.favorites_file.read_bytes()

        class RejectingProtector(TestProtector):
            def unprotect(self, ciphertext: bytes) -> bytes:
                raise ValueError("wrong Windows user")

        unreadable_store = FavoriteCacheStore(
            ProtectedStore.favorites(self.paths, RejectingProtector())
        )
        service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=unreadable_store,
            client_factory=lambda _cookies: self.fail("restore went online"),
            clock=lambda: FIXED_TIME,
        )

        with self.assertRaises(FavoritesLocalDataError):
            service.restore(service.start_operation())

        self.assertEqual(self.paths.favorites_file.read_bytes(), original)
        self.assertIsNone(service.snapshot)

    def test_duplicate_page_content_and_changing_totals_are_rejected(self):
        cases = []

        duplicate = FakeJmAccountClient(
            folders={
                "0": (
                    "Default",
                    (("1", {"name": "One"}), ("1", {"name": "Again"})),
                )
            },
            page_size=1,
        )
        cases.append(duplicate)

        changing = FakeJmAccountClient(
            folders={
                "0": (
                    "Default",
                    (("1", {"name": "One"}), ("2", {"name": "Two"})),
                )
            },
            page_size=1,
        )
        original = changing.favorite_folder

        def changing_total(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("page", args[0] if args else 1) == 2:
                return replace(result, total=3, page_count=3)
            return result

        changing.favorite_folder = changing_total
        cases.append(changing)

        duplicate_folders = FakeJmAccountClient(
            folders={"0": ("Default", ())}
        )
        original_folders = duplicate_folders.favorite_folder

        def duplicate_folder_list(*args, **kwargs):
            result = original_folders(*args, **kwargs)
            return replace(
                result,
                folder_list=result.folder_list + result.folder_list,
            )

        duplicate_folders.favorite_folder = duplicate_folder_list
        cases.append(duplicate_folders)

        for client in cases:
            with self.subTest(client=client):
                service = self.service(client)
                with self.assertRaises(FavoritesResponseError):
                    self.sync(service)
                self.assertFalse(self.paths.favorites_file.exists())

    def test_upstream_shape_changes_fail_closed_and_keep_old_cache(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        old_bytes = self.paths.favorites_file.read_bytes()

        invalid_content = FakeJmAccountClient(
            folders={"0": ("Default", (("1", {"name": "One"}),))}
        )
        original_content = invalid_content.favorite_folder

        def return_mapping_content(*args, **kwargs):
            page = original_content(*args, **kwargs)
            return replace(page, content={"unexpected": "mapping"})

        invalid_content.favorite_folder = return_mapping_content

        invalid_count = FakeJmAccountClient(
            folders={"0": ("Default", (("1", {"name": "One"}),))}
        )
        original_count = invalid_count.favorite_folder

        def return_text_page_count(*args, **kwargs):
            page = original_count(*args, **kwargs)
            return replace(page, page_count="not-a-number")

        invalid_count.favorite_folder = return_text_page_count

        invalid_metadata = FakeJmAccountClient(
            folders={
                "0": (
                    "Default",
                    (("1", {"name": "One", "tags": {"bad": "shape"}}),),
                )
            }
        )
        overlong_title = FakeJmAccountClient(
            folders={
                "0": (
                    "Default",
                    (("1", {"name": "x" * 4_097}),),
                )
            }
        )

        for label, client in (
            ("mapping content", invalid_content),
            ("nonnumeric page count", invalid_count),
            ("mapping metadata", invalid_metadata),
            ("overlong title", overlong_title),
        ):
            with self.subTest(case=label):
                service = self.service(client)
                service.restore(service.start_operation())
                with self.assertRaises(FavoritesResponseError):
                    self.sync(service)
                self.assertEqual(
                    self.paths.favorites_file.read_bytes(),
                    old_bytes,
                )
                self.assertEqual(service.snapshot, old_cache.to_snapshot())

    def test_logout_after_cache_save_does_not_recreate_old_cache(self):
        old_cache = self._sample_cache()
        self.cache_store.save(old_cache)
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = self.service(client)
        original_save = self.cache_store.save
        save_count = 0

        def save_then_logout(cache):
            nonlocal save_count
            save_count += 1
            original_save(cache)
            if save_count == 1:
                self.account_service.prepare_logout()

        self.cache_store.save = save_then_logout

        with self.assertRaises(FavoritesOperationCancelled):
            self.sync(service)

        self.assertFalse(self.paths.favorites_file.exists())

    def test_concurrent_logout_waits_for_cache_commit_and_removes_both_files(self):
        self.cache_store.save(self._sample_cache())
        client = FakeJmAccountClient(folders=self.populated_folders())
        service = self.service(client)
        original_save = self.cache_store.save
        cache_written = threading.Event()
        release_save = threading.Event()
        sync_errors = []
        logout_errors = []

        def blocking_save(cache):
            original_save(cache)
            cache_written.set()
            release_save.wait(timeout=3)

        self.cache_store.save = blocking_save
        sync_operation = service.start_operation()
        sync_thread = threading.Thread(
            target=lambda: self._capture_error(
                sync_errors,
                service.sync,
                sync_operation,
            )
        )
        sync_thread.start()
        self.assertTrue(cache_written.wait(timeout=1))

        logout_operation = self.account_service.prepare_logout()
        logout_thread = threading.Thread(
            target=lambda: self._capture_error(
                logout_errors,
                self.account_service.logout,
                logout_operation,
            )
        )
        logout_thread.start()
        release_save.set()
        sync_thread.join(timeout=2)
        logout_thread.join(timeout=2)

        self.assertFalse(sync_thread.is_alive())
        self.assertFalse(logout_thread.is_alive())
        self.assertEqual(len(sync_errors), 1)
        self.assertIsInstance(sync_errors[0], FavoritesOperationCancelled)
        self.assertEqual(logout_errors, [])
        self.assertFalse(self.paths.account_file.exists())
        self.assertFalse(self.paths.favorites_file.exists())

    def test_cache_account_mismatch_and_unknown_fields_fail_closed(self):
        cache = replace(self._sample_cache(), account_uid="20002")
        self.cache_store.save(cache)
        raw = self.paths.favorites_file.read_bytes()
        service = self.service(FakeJmAccountClient())

        with self.assertRaises(FavoritesAccountMismatch):
            service.restore(service.start_operation())

        self.assertEqual(self.paths.favorites_file.read_bytes(), raw)

        payload = self._sample_cache().to_payload()
        payload["unexpected"] = "value"
        self.favorites_protected.save(payload)
        raw = self.paths.favorites_file.read_bytes()
        with self.assertRaises(FavoritesLocalDataError):
            service.restore(service.start_operation())
        self.assertEqual(self.paths.favorites_file.read_bytes(), raw)

    def _sample_cache(self):
        return FavoritesCache(
            "10001",
            "2026-07-16T12:00:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "Default",
                    (FavoriteItemSnapshot("1", "Old item"),),
                ),
            ),
        )

    @staticmethod
    def _capture_error(errors, function, *args):
        try:
            function(*args)
        except Exception as error:
            errors.append(error)


class FavoriteCacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.protected = ProtectedStore.favorites(
            self.paths,
            TestProtector(),
        )
        self.store = FavoriteCacheStore(self.protected)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cache_payload_round_trip_is_strict_and_immutable(self):
        cache = FavoritesCache(
            "10001",
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "Default",
                    (
                        FavoriteItemSnapshot(
                            "1449491",
                            "Title",
                            ("Author",),
                            ("Tag",),
                        ),
                    ),
                ),
            ),
        )

        self.store.save(cache)
        restored = self.store.load("10001")

        self.assertEqual(restored, cache)
        self.assertEqual(restored.to_snapshot().folders, cache.folders)
        raw = self.paths.favorites_file.read_bytes()
        for secret in (b"10001", b"Title", b"Author", b"Tag"):
            self.assertNotIn(secret, raw)

    def test_invalid_cache_item_types_and_duplicates_are_rejected(self):
        base = {
            "schema_version": 1,
            "account_uid": "10001",
            "synced_at_utc": "2026-07-16T12:45:00Z",
            "folders": [
                {
                    "folder_id": "0",
                    "name": "Default",
                    "items": [
                        {
                            "album_id": "1",
                            "title": "Title",
                            "authors": [],
                            "tags": [],
                        }
                    ],
                }
            ],
        }
        invalid_payloads = []
        wrong_type = {
            **base,
            "folders": [{**base["folders"][0], "items": "not-a-list"}],
        }
        invalid_payloads.append(wrong_type)
        duplicate = {
            **base,
            "folders": [
                {
                    **base["folders"][0],
                    "items": base["folders"][0]["items"] * 2,
                }
            ],
        }
        invalid_payloads.append(duplicate)
        noncanonical_id = {
            **base,
            "folders": [
                {
                    **base["folders"][0],
                    "items": [
                        {
                            **base["folders"][0]["items"][0],
                            "album_id": "0001",
                        }
                    ],
                }
            ],
        }
        invalid_payloads.append(noncanonical_id)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.protected.save(payload)
                raw = self.paths.favorites_file.read_bytes()
                with self.assertRaises(FavoritesLocalDataError):
                    self.store.load("10001")
                self.assertEqual(self.paths.favorites_file.read_bytes(), raw)

    def test_store_refuses_cache_without_default_folder(self):
        invalid = FavoritesCache(
            "10001",
            "2026-07-16T12:45:00Z",
            (),
        )

        with self.assertRaises(FavoritesStorageError):
            self.store.save(invalid)

        self.assertFalse(self.paths.favorites_file.exists())


if __name__ == "__main__":
    unittest.main()
