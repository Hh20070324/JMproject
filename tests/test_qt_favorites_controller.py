from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import FavoriteCacheStore, FavoritesService
from jm_downloader.models import AccountStatus, FavoritesSyncProgress
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.controllers.favorites_controller import FavoritesController
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 16, 15, 30, tzinfo=timezone.utc)


class ControllerProtector:
    PREFIX = b"favorites-controller\0"

    def protect(self, plaintext):
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid")
        return ciphertext[len(self.PREFIX) :][::-1]


class FavoritesControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["favorites-controller-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = ControllerProtector()
        self.account_protected = ProtectedStore.account(self.paths, protector)
        self.favorites_protected = ProtectedStore.favorites(self.paths, protector)
        login_client = FakeJmAccountClient()
        first_service = AccountService(
            self.paths,
            account_store=AccountStore(self.account_protected),
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: login_client,
            clock=lambda: FIXED_TIME,
        )
        first_service.login(
            "test-user",
            "test-password",
            first_service.start_operation(),
        )
        self.account_service = AccountService(
            self.paths,
            account_store=AccountStore(self.account_protected),
            favorites_store=self.favorites_protected,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        self.account_service.restore(self.account_service.start_operation())
        self.account_controller = AccountController(
            self.account_service,
            result_interval_ms=2,
            auto_restore=False,
        )
        self.client = FakeJmAccountClient(
            folders={
                "0": (
                    "Default",
                    (
                        ("1", {"name": "One"}),
                        ("2", {"name": "Two"}),
                        ("3", {"name": "Three"}),
                    ),
                ),
                "9": ("Other", (("4", {"name": "Four"}),)),
            },
            page_size=2,
        )
        self.favorites_service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=FavoriteCacheStore(self.favorites_protected),
            client_factory=lambda _cookies: self.client,
            clock=lambda: FIXED_TIME,
        )
        self.controller = FavoritesController(
            self.favorites_service,
            self.account_controller,
            result_interval_ms=2,
        )

    def tearDown(self):
        self.controller.dispose()
        self.account_controller.dispose()
        self.controller.deleteLater()
        self.account_controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    def test_startup_restore_is_background_and_offline(self):
        deliveries = []
        threads = []
        self.controller.snapshot_changed.connect(
            lambda snapshot: (
                deliveries.append(snapshot),
                threads.append(QThread.currentThread()),
            )
        )

        self.assertTrue(self.wait_until(lambda: bool(deliveries)))

        self.assertIsNone(deliveries[-1].synced_at_utc)
        self.assertEqual(self.client.calls, [])
        self.assertIs(threads[-1], self.app.thread())
        self.assertTrue(self.controller.worker_is_daemon)

    def test_manual_sync_delivers_progress_snapshot_and_account_validation(self):
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )
        snapshots = []
        progress = []
        account_states = []
        self.controller.snapshot_changed.connect(snapshots.append)
        self.controller.progress_changed.connect(progress.append)
        self.account_controller.snapshot_changed.connect(
            lambda snapshot: account_states.append(snapshot.status)
        )

        generation = self.controller.sync()

        self.assertIsNotNone(generation)
        self.assertTrue(
            self.wait_until(
                lambda: bool(snapshots)
                and snapshots[-1].synced_at_utc
                == "2026-07-16T15:30:00Z"
            )
        )
        self.assertTrue(
            any(isinstance(item, FavoritesSyncProgress) for item in progress)
        )
        self.assertEqual(len(snapshots[-1].folders), 2)
        self.assertEqual(
            self.account_service.snapshot.status,
            AccountStatus.SIGNED_IN,
        )
        self.assertIn(AccountStatus.SIGNED_IN, account_states)
        self.assertTrue(self.paths.favorites_file.is_file())

    def test_stop_is_cooperative_nonblocking_and_discards_late_result(self):
        started = threading.Event()
        release = threading.Event()
        original = self.client.favorite_folder

        def slow_page(*args, **kwargs):
            page = kwargs.get("page", 1)
            if page == 2:
                started.set()
                release.wait(timeout=3)
            return original(*args, **kwargs)

        self.client.favorite_folder = slow_page
        failures = []
        snapshots = []
        self.controller.operation_failed.connect(
            lambda code, message: failures.append((code, message))
        )
        self.controller.snapshot_changed.connect(snapshots.append)
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )

        self.controller.sync()
        self.assertTrue(started.wait(timeout=1))
        before = time.monotonic()
        self.controller.cancel_sync()
        elapsed = time.monotonic() - before
        release.set()
        self.assertTrue(self.wait_until(lambda: not self.controller.is_busy))
        self.app.processEvents()

        self.assertLess(elapsed, 0.1)
        self.assertEqual(failures[-1][0], "cancelled")
        self.assertFalse(self.paths.favorites_file.exists())
        self.assertFalse(any(item.synced_at_utc for item in snapshots))

    def test_logout_cancels_sync_and_clears_memory_and_files(self):
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )
        self.controller.sync()
        self.assertTrue(
            self.wait_until(lambda: self.paths.favorites_file.exists())
        )
        cleared = []
        self.controller.snapshot_changed.connect(cleared.append)

        self.account_controller.logout()

        self.assertTrue(
            self.wait_until(
                lambda: self.account_controller.current_snapshot.status
                is AccountStatus.SIGNED_OUT
                and not self.paths.account_file.exists()
                and not self.paths.favorites_file.exists()
            )
        )
        self.assertIsNone(self.controller.current_snapshot)
        self.assertIn(None, cleared)

    def test_dispose_is_nonblocking_during_network_request(self):
        started = threading.Event()
        release = threading.Event()
        original = self.client.favorite_folder

        def slow_page(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.client.favorite_folder = slow_page
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )
        self.controller.sync()
        self.assertTrue(started.wait(timeout=1))

        before = time.monotonic()
        self.controller.dispose()
        elapsed = time.monotonic() - before
        release.set()

        self.assertLess(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
