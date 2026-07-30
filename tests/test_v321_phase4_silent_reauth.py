from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.credentials import CredentialStore
from jm_downloader.favorites import FavoriteCacheStore, FavoritesService
from jm_downloader.models import AccountStatus
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import (
    AccountController,
    _AccountJob,
)
from jm_downloader.qt.controllers.favorites_controller import (
    FavoritesController,
)
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


class ControllerProtector:
    PREFIX = b"v321-silent-reauth\0"

    def protect(self, plaintext):
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid")
        return ciphertext[len(self.PREFIX) :][::-1]


class ClientQueue:
    def __init__(self, clients):
        self.clients = list(clients)

    def __call__(self, _cookies):
        if not self.clients:
            raise AssertionError("unexpected client construction")
        return self.clients.pop(0)


class V321SilentReauthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v321-silent-reauth-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = ControllerProtector()
        self.account_store = AccountStore(
            ProtectedStore.account(self.paths, protector)
        )
        self.favorites_store = ProtectedStore.favorites(
            self.paths,
            protector,
        )
        self.credential_store = CredentialStore(
            ProtectedStore.credentials(self.paths, protector)
        )
        bootstrap = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_store,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        bootstrap.login(
            "test-user",
            "test-password",
            bootstrap.start_operation(),
        )
        self.credential_store.save("test-user", "test-password")
        self.controllers = []

    def tearDown(self):
        for favorites_controller, account_controller in self.controllers:
            favorites_controller.dispose()
            account_controller.dispose()
            for worker in (
                favorites_controller._worker,
                favorites_controller._filter_worker,
                *account_controller._workers,
            ):
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            favorites_controller.deleteLater()
            account_controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def make_controllers(
        self,
        favorites_clients,
        *,
        login_client=None,
        remember=True,
        with_credentials=True,
    ):
        if not with_credentials:
            self.credential_store.delete()
        login_client = login_client or FakeJmAccountClient()
        account_service = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_store,
            client_factory=lambda _cookies: login_client,
            clock=lambda: FIXED_TIME,
        )
        account_service.restore(account_service.start_operation())
        account_controller = AccountController(
            account_service,
            result_interval_ms=2,
            auto_restore=False,
            credential_store=self.credential_store,
            remember_credentials=remember,
        )
        client_queue = ClientQueue(favorites_clients)
        favorites_service = FavoritesService(
            account_service,
            self.paths,
            cache_store=FavoriteCacheStore(self.favorites_store),
            client_factory=client_queue,
            clock=lambda: FIXED_TIME,
        )
        favorites_controller = FavoritesController(
            favorites_service,
            account_controller,
            result_interval_ms=2,
        )
        self.controllers.append(
            (favorites_controller, account_controller)
        )
        self.assertTrue(
            self.wait_until(
                lambda: (
                    favorites_controller.current_snapshot is not None
                    and not favorites_controller.is_busy
                )
            )
        )
        return favorites_controller, account_controller, login_client

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    @staticmethod
    def expired_client():
        client = FakeJmAccountClient()
        client.favorite_errors[("0", 1)] = PermissionError("expired")
        return client

    def test_expired_read_logs_in_once_and_retries_a_fresh_sync_once(self):
        first = self.expired_client()
        recovered = FakeJmAccountClient()
        favorites, account, login_client = self.make_controllers(
            [first, recovered]
        )

        favorites.sync("mp")

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and favorites.current_snapshot is not None
                    and favorites.current_snapshot.order_by == "mp"
                )
            )
        )
        self.assertEqual(login_client.calls, [("login", "test-user")])
        self.assertEqual(account.current_snapshot.status, AccountStatus.SIGNED_IN)
        self.assertTrue(
            any(call[0] == "favorite_folder" for call in recovered.calls)
        )

    def test_second_expiration_stops_without_another_login(self):
        favorites, account, login_client = self.make_controllers(
            [self.expired_client(), self.expired_client()]
        )
        failures = []
        favorites.operation_failed.connect(lambda *args: failures.append(args))

        favorites.sync()

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                    and len(failures) >= 2
                )
            )
        )
        self.assertEqual(login_client.calls, [("login", "test-user")])

    def test_network_failure_never_triggers_silent_login(self):
        unavailable = FakeJmAccountClient()
        unavailable.favorite_errors[("0", 1)] = TimeoutError("offline")
        favorites, account, login_client = self.make_controllers([unavailable])

        favorites.sync()

        self.assertTrue(
            self.wait_until(
                lambda: not favorites.is_busy and not account.is_busy
            )
        )
        self.assertEqual(login_client.calls, [])
        self.assertNotEqual(
            account.current_snapshot.status,
            AccountStatus.EXPIRED,
        )

    def test_missing_credentials_keeps_manual_expired_flow(self):
        favorites, account, login_client = self.make_controllers(
            [self.expired_client()],
            with_credentials=False,
        )

        favorites.sync()

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                )
            )
        )
        self.assertEqual(login_client.calls, [])

    def test_write_request_is_not_replayed_after_session_recovery(self):
        write_client = FakeJmAccountClient()
        write_client.favorite_add_error = PermissionError("expired")
        refreshed = FakeJmAccountClient()
        favorites, account, login_client = self.make_controllers(
            [write_client, refreshed]
        )

        favorites.add_album("88")

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and favorites.current_snapshot is not None
                )
            )
        )
        add_calls = [
            call
            for call in write_client.calls
            if call[0] == "add_favorite_album"
        ]
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(login_client.calls, [("login", "test-user")])
        self.assertFalse(
            any(call[0] == "add_favorite_album" for call in refreshed.calls)
        )

    def test_confirmed_write_retries_only_the_failed_refresh_read(self):
        write_client = FakeJmAccountClient()
        expired_refresh = self.expired_client()
        recovered_refresh = FakeJmAccountClient()
        favorites, account, login_client = self.make_controllers(
            [write_client, expired_refresh, recovered_refresh]
        )

        favorites.add_album("88")

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and favorites.current_snapshot is not None
                    and account.current_snapshot.status
                    is AccountStatus.SIGNED_IN
                )
            )
        )
        add_calls = [
            call
            for client in (write_client, expired_refresh, recovered_refresh)
            for call in client.calls
            if call[0] == "add_favorite_album"
        ]
        self.assertEqual(add_calls, [("add_favorite_album", "88", "0")])
        self.assertEqual(login_client.calls, [("login", "test-user")])
        self.assertTrue(
            any(
                call[0] == "favorite_folder"
                for call in recovered_refresh.calls
            )
        )

    def test_different_account_is_rejected_without_overwriting_session(self):
        old_file = self.paths.account_file.read_bytes()
        wrong_account = FakeJmAccountClient(uid="20002")
        favorites, account, login_client = self.make_controllers(
            [self.expired_client()],
            login_client=wrong_account,
        )

        favorites.sync()

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                )
            )
        )
        self.assertEqual(login_client.calls, [("login", "test-user")])
        self.assertEqual(self.paths.account_file.read_bytes(), old_file)

    def test_silent_job_repr_hides_and_clear_removes_password(self):
        job = _AccountJob(
            1,
            1,
            "silent_reauth",
            "test-user",
            "very-secret",
            False,
            "10001",
        )

        self.assertNotIn("very-secret", repr(job))
        job.clear_secret()
        self.assertIsNone(job.password)


if __name__ == "__main__":
    unittest.main()
