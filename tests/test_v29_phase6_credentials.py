import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.credentials import (
    CredentialStore,
    RememberedCredentials,
)
from jm_downloader.models import AccountStatus
from jm_downloader.protected_store import (
    ProtectedStore,
    ProtectedStoreDeleteError,
)
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


class CredentialProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"credentials\0" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix = b"credentials\0"
        if not ciphertext.startswith(prefix):
            raise ValueError("invalid ciphertext")
        return ciphertext[len(prefix) :][::-1]


class Phase6CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["phase6-credential-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.protector = CredentialProtector()
        self.credential_store = CredentialStore(
            ProtectedStore.credentials(self.paths, self.protector)
        )
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.dispose()
            for worker in controller._workers:
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def make_controller(
        self,
        client=None,
        *,
        remember_credentials=True,
        credential_store=None,
    ):
        account_store = AccountStore(
            ProtectedStore.account(self.paths, self.protector)
        )
        favorites_store = ProtectedStore.favorites(
            self.paths,
            self.protector,
        )
        service = AccountService(
            self.paths,
            account_store=account_store,
            favorites_store=favorites_store,
            client_factory=lambda _cookies: client
            or FakeJmAccountClient(),
        )
        controller = AccountController(
            service,
            result_interval_ms=5,
            auto_restore=False,
            credential_store=credential_store or self.credential_store,
            remember_credentials=remember_credentials,
        )
        self.controllers.append(controller)
        return controller

    def test_encrypted_round_trip_hides_plaintext(self):
        self.credential_store.save(
            "remembered-user",
            "private-password",
        )

        raw = self.paths.credentials_file.read_bytes()
        self.assertNotIn(b"remembered-user", raw)
        self.assertNotIn(b"private-password", raw)
        self.assertEqual(
            self.credential_store.load(),
            RememberedCredentials(
                "remembered-user",
                "private-password",
            ),
        )

    def test_corrupt_file_is_backed_up_and_treated_as_empty(self):
        self.paths.credentials_file.write_bytes(b"not-an-envelope")

        self.assertIsNone(self.credential_store.load())

        backup = self.credential_store.last_recovery_backup
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertFalse(self.paths.credentials_file.exists())

    def test_disabled_startup_removes_legacy_credentials(self):
        self.credential_store.save("test-user", "test-password")

        self.make_controller(remember_credentials=False)

        self.assertFalse(self.paths.credentials_file.exists())

    def test_enabled_startup_exposes_fill_signal_without_login(self):
        self.credential_store.save("test-user", "test-password")
        client = FakeJmAccountClient()
        controller = self.make_controller(client)
        received = []
        controller.credentials_ready.connect(
            lambda username, password: received.append(
                (username, password)
            )
        )

        self.assertTrue(controller.request_credentials())

        self.assertEqual(
            received,
            [("test-user", "test-password")],
        )
        self.assertEqual(client.calls, [])

    def test_successful_login_updates_credentials(self):
        controller = self.make_controller()

        controller.login("test-user", "test-password")
        self.assertTrue(
            self.wait_until(
                lambda: not controller.is_busy
                and controller.current_snapshot.status
                is AccountStatus.SIGNED_IN
            )
        )

        self.assertEqual(
            self.credential_store.load(),
            RememberedCredentials("test-user", "test-password"),
        )

    def test_login_failure_preserves_existing_credentials(self):
        self.credential_store.save("test-user", "test-password")
        client = FakeJmAccountClient()
        client.login_error = RuntimeError("network unavailable")
        controller = self.make_controller(client)

        controller.login("test-user", "wrong-password")
        self.assertTrue(self.wait_until(lambda: not controller.is_busy))

        self.assertEqual(
            self.credential_store.load(),
            RememberedCredentials("test-user", "test-password"),
        )

    def test_logout_and_disabling_delete_credentials(self):
        controller = self.make_controller()
        controller.login("test-user", "test-password")
        self.assertTrue(
            self.wait_until(
                lambda: controller.current_snapshot.status
                is AccountStatus.SIGNED_IN
            )
        )

        controller.logout()
        self.assertTrue(self.wait_until(lambda: not controller.is_busy))
        self.assertFalse(self.paths.credentials_file.exists())

        self.credential_store.save("test-user", "test-password")
        self.assertTrue(controller.set_remember_credentials(False))
        self.assertFalse(self.paths.credentials_file.exists())

    def test_delete_failure_is_reported(self):
        controller = self.make_controller()
        failures = []
        controller.credential_failed.connect(failures.append)

        with patch.object(
            self.credential_store,
            "delete",
            side_effect=ProtectedStoreDeleteError("private path"),
        ):
            self.assertFalse(
                controller.set_remember_credentials(False)
            )

        self.assertEqual(
            failures,
            ["无法删除本地安全凭据 credentials.dat"],
        )
        self.assertNotIn("private path", failures[0])

    def wait_until(self, predicate, timeout_ms=2000):
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        return predicate()


if __name__ == "__main__":
    unittest.main()
