from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from jm_downloader.account import (
    AccountResponseError,
    AccountService,
    AccountStorageError,
    AccountStore,
)
from jm_downloader.favorites import (
    FavoriteCacheStore,
    FavoritesService,
    FavoritesStorageError,
    FavoritesUnavailable,
)
from jm_downloader.models import AccountStatus
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient
from tests.test_favorites import FIXED_TIME, TestProtector


class CookieJar:
    def __init__(self, values):
        self.values = dict(values)

    def get_dict(self):
        return dict(self.values)


class AsyncFavoritesClient:
    def __init__(
        self,
        inner,
        *,
        cookies=None,
        setup_error=None,
        request_error=None,
    ):
        self.inner = inner
        self.setup_error = setup_error
        self.request_error = request_error
        self.setup_called = 0
        self.close_called = 0
        self._session = SimpleNamespace(
            cookies=CookieJar(
                cookies
                or {"AVS": "test-avs", "session": "test-cookie"}
            )
        )

    async def setup(self):
        self.setup_called += 1
        if self.setup_error is not None:
            raise self.setup_error

    async def close(self):
        self.close_called += 1

    async def favorite_folder(self, **kwargs):
        if self.request_error is not None:
            raise self.request_error
        return self.inner.favorite_folder(**kwargs)


class V321AsyncFavoritesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = TestProtector()
        self.account_store = AccountStore(
            ProtectedStore.account(self.paths, protector)
        )
        self.favorites_store = ProtectedStore.favorites(
            self.paths,
            protector,
        )
        self.cache_store = FavoriteCacheStore(self.favorites_store)
        self.sync_client = FakeJmAccountClient()
        self.account = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_store,
            client_factory=lambda _cookies: self.sync_client,
            clock=lambda: FIXED_TIME,
        )
        operation = self.account.start_operation()
        self.account.login("test-user", "test-password", operation)

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self, active, *, engine="async"):
        return FavoritesService(
            self.account,
            self.paths,
            cache_store=self.cache_store,
            client_factory=lambda _cookies: self.sync_client,
            clock=lambda: FIXED_TIME,
            query_engine_provider=lambda: engine,
            async_client_factory=lambda _cookies, _route: active,
        )

    def test_async_and_sync_read_paths_publish_equivalent_snapshots(self):
        folders = {
            "0": (
                "Default",
                (
                    ("1", {"name": "First"}),
                    ("2", {"name": "Second"}),
                ),
            ),
            "8": ("Custom", (("3", {"name": "Third"}),)),
        }
        sync_client = FakeJmAccountClient(folders=folders, page_size=1)
        sync_service = FavoritesService(
            self.account,
            self.paths,
            cache_store=self.cache_store,
            client_factory=lambda _cookies: sync_client,
            clock=lambda: FIXED_TIME,
        )
        sync_snapshot = sync_service.sync(
            sync_service.start_operation(),
            query_engine="sync",
        )

        async_inner = FakeJmAccountClient(folders=folders, page_size=1)
        active = AsyncFavoritesClient(async_inner)
        async_service = self.service(active)
        async_snapshot = async_service.sync(
            async_service.start_operation(),
            query_engine="async",
        )

        self.assertEqual(async_snapshot, sync_snapshot)
        self.assertEqual(active.setup_called, 1)
        self.assertEqual(active.close_called, 1)
        self.assertEqual(async_inner.calls, sync_client.calls)

    def test_refreshed_valid_cookies_are_filtered_and_written_once(self):
        active = AsyncFavoritesClient(
            FakeJmAccountClient(),
            cookies={
                "AVS": "renewed-avs",
                "session": "renewed-session",
                "unrelated": "ignored",
            },
        )
        service = self.service(active)
        old = self.account.current_session()

        with patch.object(
            self.account_store,
            "save",
            wraps=self.account_store.save,
        ) as save:
            service.sync(service.start_operation(), query_engine="async")

        updated = self.account.current_session()
        self.assertEqual(updated.uid, old.uid)
        self.assertEqual(updated.username, old.username)
        self.assertEqual(
            updated.cookie_dict(),
            {"AVS": "renewed-avs", "session": "renewed-session"},
        )
        save.assert_called_once_with(updated)
        self.assertEqual(self.account.snapshot.status, AccountStatus.SIGNED_IN)

    def test_unchanged_cookies_do_not_rewrite_account_file(self):
        active = AsyncFavoritesClient(FakeJmAccountClient())
        service = self.service(active)
        before = self.paths.account_file.read_bytes()

        with patch.object(self.account_store, "save") as save:
            service.sync(service.start_operation(), query_engine="async")

        save.assert_not_called()
        self.assertEqual(self.paths.account_file.read_bytes(), before)

    def test_invalid_or_unwritable_cookie_refresh_preserves_old_session(self):
        old_session = self.account.current_session()
        old_file = self.paths.account_file.read_bytes()
        with self.assertRaises(AccountResponseError):
            self.account.update_session_cookies(
                old_session,
                {"session": "missing-avs"},
            )
        self.assertEqual(self.account.current_session(), old_session)
        self.assertEqual(self.paths.account_file.read_bytes(), old_file)

        active = AsyncFavoritesClient(
            FakeJmAccountClient(),
            cookies={"AVS": "new-avs", "session": "new-session"},
        )
        service = self.service(active)
        with (
            patch.object(
                self.account_store,
                "save",
                side_effect=AccountStorageError(),
            ),
            self.assertRaises(FavoritesStorageError),
        ):
            service.sync(service.start_operation(), query_engine="async")
        self.assertEqual(self.account.current_session(), old_session)
        self.assertEqual(self.paths.account_file.read_bytes(), old_file)

    def test_async_failures_close_and_preserve_the_previous_cache(self):
        initial = self.service(
            AsyncFavoritesClient(FakeJmAccountClient())
        )
        old_snapshot = initial.sync(
            initial.start_operation(),
            query_engine="async",
        )
        old_file = self.paths.favorites_file.read_bytes()

        failing = AsyncFavoritesClient(
            FakeJmAccountClient(),
            request_error=TimeoutError("private endpoint"),
        )
        service = self.service(failing)
        service.restore(service.start_operation())
        with self.assertRaises(FavoritesUnavailable) as caught:
            service.sync(service.start_operation(), query_engine="async")

        self.assertNotIn("private endpoint", str(caught.exception))
        self.assertEqual(failing.close_called, 1)
        self.assertEqual(service.snapshot, old_snapshot)
        self.assertEqual(self.paths.favorites_file.read_bytes(), old_file)


if __name__ == "__main__":
    unittest.main()
