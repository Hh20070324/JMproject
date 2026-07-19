from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import (
    FavoriteCacheStore,
    FavoritesService,
    FavoritesSessionExpired,
)
from jm_downloader.models import (
    AccountSnapshot,
    AccountStatus,
    FavoritesSyncProgress,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.controllers.favorites_controller import FavoritesController
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeFavoriteAddResponse, FakeJmAccountClient


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

    def test_sync_rebuilds_an_immutable_known_favorite_id_set(self):
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )
        deliveries = []
        self.controller.known_favorite_ids_changed.connect(deliveries.append)

        self.controller.sync()

        self.assertTrue(
            self.wait_until(
                lambda: self.controller.known_favorite_ids
                == frozenset({"1", "2", "3", "4"})
            )
        )
        self.assertIsInstance(deliveries[-1], frozenset)

    def test_add_is_background_dedicated_and_does_not_write_the_cache(self):
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )
        successes = []
        failures = []
        generic_failures = []
        availability = []
        delivery_threads = []
        self.controller.add_succeeded.connect(
            lambda album_id: (
                successes.append(album_id),
                delivery_threads.append(QThread.currentThread()),
            )
        )
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        self.controller.operation_failed.connect(
            lambda *args: generic_failures.append(args)
        )
        self.controller.add_availability_changed.connect(availability.append)

        generation = self.controller.add_album(" JM 0777777 ")

        self.assertIsNotNone(generation)
        self.assertFalse(self.controller.can_add_favorites)
        self.assertEqual(self.controller.current_command, "add")
        self.assertTrue(self.wait_until(lambda: successes == ["777777"]))
        self.assertEqual(failures, [])
        self.assertEqual(generic_failures, [])
        self.assertEqual(
            self.client.calls,
            [("add_favorite_album", "777777", "0")],
        )
        self.assertIn("777777", self.controller.known_favorite_ids)
        self.assertIs(delivery_threads[-1], self.app.thread())
        self.assertEqual(availability, [False, True])
        self.assertTrue(self.controller.can_add_favorites)
        self.assertEqual(
            self.account_controller.current_snapshot.status,
            AccountStatus.SIGNED_IN,
        )
        self.assertFalse(self.paths.favorites_file.exists())

    def test_add_and_sync_are_serial_and_repeated_add_is_rejected(self):
        started = threading.Event()
        release = threading.Event()
        original_add = self.client.add_favorite_album

        def slow_add(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return original_add(*args, **kwargs)

        self.client.add_favorite_album = slow_add
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        first = self.controller.add_album("777777")
        self.assertTrue(started.wait(timeout=1))
        repeated = self.controller.add_album("777777")
        sync_while_adding = self.controller.sync()
        release.set()
        self.assertTrue(self.wait_until(lambda: not self.controller.is_busy))

        self.assertIsNotNone(first)
        self.assertIsNone(repeated)
        self.assertIsNone(sync_while_adding)
        self.assertEqual(
            [
                call
                for call in self.client.calls
                if call[0] == "add_favorite_album"
            ],
            [("add_favorite_album", "777777", "0")],
        )
        self.assertIsNone(self.controller.add_album("777777"))

    def test_sync_busy_rejects_an_add_request(self):
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
        add_while_syncing = self.controller.add_album("777777")
        release.set()
        self.assertTrue(self.wait_until(lambda: not self.controller.is_busy))

        self.assertIsNone(add_while_syncing)
        self.assertFalse(
            any(call[0] == "add_favorite_album" for call in self.client.calls)
        )

    def test_add_failure_uses_only_the_dedicated_safe_signal(self):
        secret = "private-upstream-response-and-url"
        self.client.favorite_add_error = TimeoutError(secret)
        failures = []
        generic_failures = []
        successes = []
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        self.controller.operation_failed.connect(
            lambda *args: generic_failures.append(args)
        )
        self.controller.add_succeeded.connect(successes.append)
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")

        self.assertTrue(self.wait_until(lambda: bool(failures)))
        self.assertEqual(
            failures,
            [("777777", "add_uncertain", "收藏结果无法确认，请手动同步")],
        )
        self.assertNotIn(secret, repr(failures))
        self.assertEqual(generic_failures, [])
        self.assertEqual(successes, [])
        self.assertNotIn("777777", self.controller.known_favorite_ids)
        self.assertTrue(self.controller.can_add_favorites)

    def test_remove_toggle_is_reported_without_marking_card_favorited(self):
        self.client.favorite_add_response = FakeFavoriteAddResponse(
            type="Remove"
        )
        failures = []
        successes = []
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        self.controller.add_succeeded.connect(successes.append)
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")

        self.assertTrue(self.wait_until(lambda: bool(failures)))
        self.assertEqual(
            failures,
            [
                (
                    "777777",
                    "removed_instead_of_added",
                    "检测到该漫画已在远端收藏，本次操作已将其移除；"
                    "请手动同步收藏夹",
                )
            ],
        )
        self.assertEqual(successes, [])
        self.assertNotIn("777777", self.controller.known_favorite_ids)
        self.assertTrue(self.controller.can_add_favorites)

    def test_expired_add_updates_the_account_and_disables_future_adds(self):
        self.client.favorite_add_error = PermissionError("private response")
        failures = []
        generic_failures = []
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        self.controller.operation_failed.connect(
            lambda *args: generic_failures.append(args)
        )
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")

        self.assertTrue(
            self.wait_until(
                lambda: bool(failures)
                and self.account_controller.current_snapshot.status
                is AccountStatus.EXPIRED
            )
        )
        self.assertEqual(
            failures,
            [
                (
                    "777777",
                    FavoritesSessionExpired.code,
                    FavoritesSessionExpired.default_message,
                )
            ],
        )
        self.assertEqual(generic_failures, [])
        self.assertFalse(self.controller.can_add_favorites)
        self.assertIsNone(self.controller.add_album("888888"))

    def test_account_change_discards_a_late_add_and_clears_runtime_ids(self):
        remote_written = threading.Event()
        release = threading.Event()
        original_add = self.client.add_favorite_album

        def accepted_then_slow(*args, **kwargs):
            result = original_add(*args, **kwargs)
            remote_written.set()
            release.wait(timeout=3)
            return result

        self.client.add_favorite_album = accepted_then_slow
        successes = []
        self.controller.add_succeeded.connect(successes.append)
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")
        self.assertTrue(remote_written.wait(timeout=1))
        self.account_controller.logout()
        release.set()

        self.assertTrue(
            self.wait_until(
                lambda: self.account_controller.current_snapshot.status
                is AccountStatus.SIGNED_OUT
                and not self.account_controller.is_busy
                and not self.controller.is_busy
            )
        )
        self.app.processEvents()
        self.assertEqual(successes, [])
        self.assertEqual(self.controller.known_favorite_ids, frozenset())
        self.assertFalse(self.controller.can_add_favorites)

    def test_account_switch_discards_an_inflight_add_result(self):
        remote_written = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        original_add = self.client.add_favorite_album

        def accepted_then_slow(*args, **kwargs):
            result = original_add(*args, **kwargs)
            remote_written.set()
            release.wait(timeout=3)
            returned.set()
            return result

        self.client.add_favorite_album = accepted_then_slow
        successes = []
        failures = []
        self.controller.add_succeeded.connect(successes.append)
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")
        self.assertTrue(remote_written.wait(timeout=1))
        self.controller._on_account_snapshot(
            AccountSnapshot(AccountStatus.SIGNING_IN, "another-user")
        )
        release.set()
        self.assertTrue(returned.wait(timeout=1))
        time.sleep(0.02)
        self.app.processEvents()
        self.assertEqual(successes, [])
        self.assertEqual(failures, [])
        self.assertEqual(self.controller.known_favorite_ids, frozenset())
        self.assertFalse(self.controller.can_add_favorites)
        self.assertFalse(self.paths.favorites_file.exists())

    def test_expiry_and_account_switch_clear_session_additions(self):
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )
        self.controller.add_album("777777")
        self.assertTrue(
            self.wait_until(
                lambda: "777777" in self.controller.known_favorite_ids
            )
        )

        self.account_controller.mark_expired()
        self.app.processEvents()

        self.assertEqual(self.controller.known_favorite_ids, frozenset())
        self.assertFalse(self.controller.can_add_favorites)

        self.controller._on_account_snapshot(
            AccountSnapshot(AccountStatus.SIGNING_IN, "another-user")
        )
        self.assertEqual(self.controller.known_favorite_ids, frozenset())

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

    def test_repeated_sync_click_does_not_queue_a_second_request(self):
        started = threading.Event()
        release = threading.Event()
        original = self.client.favorite_folder

        def slow_first_page(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)

        self.client.favorite_folder = slow_first_page
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )

        first = self.controller.sync()
        self.assertTrue(started.wait(timeout=1))
        second = self.controller.sync()
        release.set()
        self.assertTrue(self.wait_until(lambda: not self.controller.is_busy))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        first_page_calls = [
            call
            for call in self.client.calls
            if call == ("favorite_folder", 1, "mr", "0", "")
        ]
        self.assertEqual(len(first_page_calls), 1)

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

        deadline = time.monotonic() + 2
        while self.controller._worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertLess(elapsed, 0.1)
        self.assertFalse(self.controller._worker.is_alive())
        self.assertFalse(self.paths.favorites_file.exists())
        self.assertIsNone(self.controller.current_snapshot.synced_at_utc)

    def test_window_close_during_add_is_nonblocking_and_drops_late_result(self):
        remote_written = threading.Event()
        release = threading.Event()
        original_add = self.client.add_favorite_album

        def accepted_then_slow(*args, **kwargs):
            result = original_add(*args, **kwargs)
            remote_written.set()
            release.wait(timeout=3)
            return result

        self.client.add_favorite_album = accepted_then_slow
        successes = []
        failures = []
        self.controller.add_succeeded.connect(successes.append)
        self.controller.add_failed.connect(lambda *args: failures.append(args))
        window = MainWindow(
            ThemeManager(),
            account_controller=self.account_controller,
            favorites_controller=self.controller,
            persist_window_state=False,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        self.app.processEvents()
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and not self.controller.is_busy
            )
        )

        self.controller.add_album("777777")
        self.assertTrue(remote_written.wait(timeout=1))
        before = time.monotonic()
        closed = window.close()
        elapsed = time.monotonic() - before
        release.set()

        deadline = time.monotonic() + 2
        while self.controller._worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.app.processEvents()

        self.assertTrue(closed)
        self.assertLess(elapsed, 0.1)
        self.assertFalse(self.controller._worker.is_alive())
        self.assertEqual(successes, [])
        self.assertEqual(failures, [])
        self.assertEqual(self.controller.known_favorite_ids, frozenset())
        self.assertFalse(self.paths.favorites_file.exists())
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
