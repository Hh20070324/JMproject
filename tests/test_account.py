from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from jm_downloader import account
from jm_downloader.account import (
    AccountError,
    AccountLocalDataError,
    AccountOperationCancelled,
    AccountService,
    AccountStorageError,
    AccountStore,
    AccountSwitchRequired,
    AccountUnavailable,
    AccountValidationError,
    validate_login_credentials,
)
from jm_downloader.models import AccountStatus
from jm_downloader.protected_store import (
    ProtectedStore,
    ProtectedStoreDeleteError,
)
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 16, 8, 30, tzinfo=timezone.utc)


class TestProtector:
    PREFIX = b"account-test\0"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid ciphertext")
        return ciphertext[len(self.PREFIX) :][::-1]


class AccountServiceTests(unittest.TestCase):
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
        self.clients = []
        self.factory_calls = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self, clients=None):
        pending = list(clients or [FakeJmAccountClient()])

        def client_factory(cookies):
            self.factory_calls.append(cookies)
            client = pending.pop(0)
            self.clients.append(client)
            return client

        return AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_protected,
            client_factory=client_factory,
            clock=lambda: FIXED_TIME,
        )

    def login(self, service, username="test-user", password="test-password"):
        operation = service.start_operation()
        return service.login(username, password, operation)

    def test_missing_restore_is_offline_and_does_not_create_files(self):
        service = self.service()
        operation = service.start_operation()

        snapshot = service.restore(operation)

        self.assertEqual(snapshot.status, AccountStatus.SIGNED_OUT)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_login_persists_only_validated_session_and_restores_offline(self):
        service = self.service()

        snapshot = self.login(service)

        self.assertEqual(snapshot.status, AccountStatus.SIGNED_IN)
        self.assertEqual(snapshot.username, "test-user")
        self.assertEqual(snapshot.last_verified_at_utc, "2026-07-16T08:30:00Z")
        self.assertFalse(hasattr(snapshot, "cookies"))
        self.assertFalse(hasattr(snapshot, "uid"))
        self.assertEqual(self.factory_calls, [None])
        self.assertTrue(self.paths.account_file.is_file())
        self.assertFalse(self.paths.settings_file.exists())
        self.assertFalse(self.paths.tasks_file.exists())
        raw = self.paths.account_file.read_bytes()
        for secret in (b"test-user", b"test-password", b"test-cookie", b"test-avs"):
            self.assertNotIn(secret, raw)

        restored = self.service(clients=[])
        restored_snapshot = restored.restore(restored.start_operation())
        self.assertEqual(restored_snapshot.status, AccountStatus.SAVED_SESSION)
        self.assertEqual(restored_snapshot.username, "test-user")
        self.assertEqual(restored.current_session().cookie_dict()["AVS"], "test-avs")

    def test_corrupt_or_unknown_account_fields_fail_closed(self):
        invalid_payloads = (
            {
                "schema_version": 1,
                "uid": "10001",
                "username": "name",
                "cookies": {"AVS": "token", "unknown": "secret"},
                "last_verified_at_utc": "2026-07-16T08:30:00Z",
            },
            {
                "schema_version": 1,
                "uid": "not-numeric",
                "username": "name",
                "cookies": {"AVS": "token"},
                "last_verified_at_utc": "2026-07-16T08:30:00Z",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.account_protected.save(payload)
                service = self.service()
                snapshot = service.restore(service.start_operation())
                self.assertEqual(
                    snapshot.status,
                    AccountStatus.LOCAL_DATA_UNREADABLE,
                )
                self.assertTrue(self.paths.account_file.is_file())

    def test_login_failure_is_sanitized_and_preserves_expired_session(self):
        first = FakeJmAccountClient()
        failing = FakeJmAccountClient()
        secret = "password-cookie-url-secret"
        failing.login_error = RuntimeError(secret)
        service = self.service([first, failing])
        self.login(service)
        before = self.paths.account_file.read_bytes()
        service.mark_expired()

        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            with self.assertRaises(AccountError) as raised:
                self.login(service)

        self.assertEqual(str(raised.exception), AccountError.default_message)
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(self.paths.account_file.read_bytes(), before)
        self.assertEqual(service.snapshot.status, AccountStatus.EXPIRED)

    def test_network_failure_maps_to_stable_category(self):
        client = FakeJmAccountClient()
        client.login_error = TimeoutError("private URL and password")
        service = self.service([client])

        with self.assertRaises(AccountUnavailable) as raised:
            self.login(service)

        self.assertEqual(raised.exception.code, "unavailable")
        self.assertNotIn("private", str(raised.exception))
        self.assertFalse(self.paths.account_file.exists())

    def test_active_session_requires_logout_before_another_login(self):
        service = self.service([FakeJmAccountClient(), FakeJmAccountClient()])
        self.login(service)

        with self.assertRaises(AccountSwitchRequired):
            self.login(service)

        self.assertEqual(len(self.clients), 1)

    def test_expired_session_can_relogin_and_new_uid_clears_old_favorites(self):
        old_client = FakeJmAccountClient(uid="10001")
        new_client = FakeJmAccountClient(uid="20002")
        service = self.service([old_client, new_client])
        self.login(service)
        self.favorites_protected.save(
            {"schema_version": 1, "uid": "10001", "folders": []}
        )
        service.mark_expired()

        snapshot = self.login(service)

        self.assertEqual(snapshot.status, AccountStatus.SIGNED_IN)
        self.assertFalse(self.paths.favorites_file.exists())
        self.assertEqual(service.current_session().uid, "20002")

    def test_generation_cancels_slow_login_before_it_can_persist(self):
        started = threading.Event()
        release = threading.Event()
        client = FakeJmAccountClient()
        original_login = client.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=3)
            return original_login(username, password)

        client.login = slow_login
        service = self.service([client])
        operation = service.start_operation()
        errors = []

        thread = threading.Thread(
            target=lambda: self._capture_error(
                errors,
                service.login,
                "test-user",
                "test-password",
                operation,
            )
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        service.invalidate_operations()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AccountOperationCancelled)
        self.assertFalse(self.paths.account_file.exists())

    def test_generation_rolls_back_file_if_cancelled_during_save(self):
        service = self.service()
        started = threading.Event()
        release = threading.Event()
        original_save = self.account_store.save

        def blocking_save(session):
            original_save(session)
            started.set()
            release.wait(timeout=3)

        self.account_store.save = blocking_save
        operation = service.start_operation()
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_error(
                errors,
                service.login,
                "test-user",
                "test-password",
                operation,
            )
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        service.invalidate_operations()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], AccountOperationCancelled)
        self.assertFalse(self.paths.account_file.exists())

    def test_logout_deletes_account_favorites_and_memory_session(self):
        service = self.service()
        self.login(service)
        self.favorites_protected.save(
            {"schema_version": 1, "uid": "10001", "folders": []}
        )
        operation = service.prepare_logout()

        snapshot = service.logout(operation)

        self.assertEqual(snapshot.status, AccountStatus.SIGNED_OUT)
        self.assertFalse(self.paths.account_file.exists())
        self.assertFalse(self.paths.favorites_file.exists())
        with self.assertRaises(AccountLocalDataError):
            service.current_session()

    def test_logout_delete_failure_reports_filename_and_disables_session(self):
        service = self.service()
        self.login(service)
        operation = service.prepare_logout()
        with patch.object(
            self.favorites_protected,
            "delete",
            side_effect=ProtectedStoreDeleteError("secret disk details"),
        ):
            with self.assertRaises(AccountStorageError) as raised:
                service.logout(operation)

        self.assertIn("favorites.dat", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(
            service.snapshot.status,
            AccountStatus.LOCAL_DATA_UNREADABLE,
        )
        with self.assertRaises(AccountLocalDataError):
            service.current_session()

    @staticmethod
    def _capture_error(errors, function, *args):
        try:
            function(*args)
        except Exception as error:
            errors.append(error)


class AccountValidationTests(unittest.TestCase):
    def test_login_credentials_are_bounded_without_modifying_password(self):
        self.assertEqual(
            validate_login_credentials("  name  ", " password "),
            ("name", " password "),
        )
        for username, password in (
            ("", "password"),
            ("name", ""),
            ("name\nother", "password"),
            ("name", "line\nbreak"),
            ("x" * 129, "password"),
            ("name", "x" * 513),
        ):
            with self.subTest(username=username[:10]):
                with self.assertRaises(AccountValidationError):
                    validate_login_credentials(username, password)


class DefaultAccountClientFactoryTests(unittest.TestCase):
    class FakeApiClient:
        def __init__(self):
            self.retry_times = None
            self.timeout = None

        def get_meta_data(self, name):
            if name == "timeout":
                return self.timeout
            return None

    class FakeOption:
        def __init__(self, client):
            self.client = Mock(retry_times=5)
            self.created_client = client
            self.calls = []

        def new_jm_client(self, **kwargs):
            self.calls.append((self.client.retry_times, kwargs))
            self.created_client.timeout = kwargs.get("timeout")
            return self.created_client

    def test_builds_isolated_api_client_with_bounded_in_memory_config(self):
        client = self.FakeApiClient()
        option = self.FakeOption(client)
        with (
            patch.object(account.jmcomic, "create_option_by_file", return_value=option),
            patch.object(account.jmcomic, "JmApiClient", self.FakeApiClient),
        ):
            result = account.build_account_client(
                Path("option.yml"),
                {"AVS": "token"},
            )

        self.assertIs(result, client)
        self.assertEqual(
            option.calls,
            [
                (
                    0,
                    {
                        "impl": "api",
                        "timeout": account.ACCOUNT_TIMEOUT_SECONDS,
                        "cookies": {"AVS": "token"},
                    },
                )
            ],
        )
        self.assertEqual(client.retry_times, account.ACCOUNT_REQUEST_RETRIES)


if __name__ == "__main__":
    unittest.main()
