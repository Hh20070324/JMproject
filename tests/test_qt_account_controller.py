import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.models import AccountStatus
from jm_downloader.protected_store import (
    ProtectedStore,
    ProtectedStoreDeleteError,
)
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


class ControllerProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"controller\0" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"controller\0"):
            raise ValueError("invalid")
        return ciphertext[len(b"controller\0") :][::-1]


class AccountControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["account-controller-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = ControllerProtector()
        self.account_store = AccountStore(
            ProtectedStore.account(self.paths, protector)
        )
        self.favorites_store = ProtectedStore.favorites(self.paths, protector)
        self.controllers = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for controller in self.controllers:
            controller.dispose()
            for worker in controller._workers:
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def make_controller(self, clients=None, *, auto_restore=False):
        pending = list(clients or [FakeJmAccountClient()])
        service = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_store,
            client_factory=lambda _cookies: pending.pop(0),
        )
        controller = AccountController(
            service,
            result_interval_ms=5,
            auto_restore=auto_restore,
        )
        self.controllers.append(controller)
        return controller

    def test_restore_runs_off_main_thread_and_delivers_on_qt_thread(self):
        controller = self.make_controller()
        worker_threads = []
        receiving_threads = []
        original_load = self.account_store.load

        def tracked_load():
            worker_threads.append(threading.current_thread())
            return original_load()

        self.account_store.load = tracked_load
        snapshots = []
        controller.snapshot_changed.connect(
            lambda snapshot: (
                snapshots.append(snapshot),
                receiving_threads.append(QThread.currentThread()),
            )
        )

        generation = controller.restore()
        self.assertEqual(generation, 1)
        self.assertTrue(
            self.wait_until(
                lambda: snapshots
                and snapshots[-1].status is AccountStatus.SIGNED_OUT
                and not controller.is_busy
            )
        )

        self.assertIsNot(worker_threads[0], threading.main_thread())
        self.assertEqual(receiving_threads[-1], self.app.thread())
        self.assertTrue(controller.worker_is_daemon)

    def test_login_publishes_transient_then_verified_snapshot(self):
        controller = self.make_controller()
        snapshots = []
        busy = []
        failures = []
        controller.snapshot_changed.connect(snapshots.append)
        controller.busy_changed.connect(busy.append)
        controller.operation_failed.connect(lambda *args: failures.append(args))

        generation = controller.login("test-user", "test-password")
        self.assertEqual(generation, 1)
        self.assertEqual(snapshots[0].status, AccountStatus.SIGNING_IN)
        self.assertTrue(
            self.wait_until(
                lambda: snapshots[-1].status is AccountStatus.SIGNED_IN
                and not controller.is_busy
            )
        )

        self.assertEqual(busy, [True, False])
        self.assertEqual(failures, [])
        self.assertTrue(self.paths.account_file.is_file())
        self.assertIsNone(controller._mailbox.pending)

    def test_invalid_credentials_do_not_create_job_or_generation(self):
        controller = self.make_controller()
        failures = []
        controller.operation_failed.connect(lambda *args: failures.append(args))

        result = controller.login("", "password")

        self.assertIsNone(result)
        self.assertEqual(controller.generation, 0)
        self.assertEqual(failures[0][0], "validation")
        self.assertFalse(controller.is_busy)

    def test_worker_failure_uses_stable_message_without_secret(self):
        client = FakeJmAccountClient()
        secret = "password-cookie-url-secret"
        client.login_error = RuntimeError(secret)
        controller = self.make_controller([client])
        failures = []
        controller.operation_failed.connect(lambda *args: failures.append(args))

        controller.login("test-user", "test-password")
        self.assertTrue(self.wait_until(lambda: bool(failures)))

        self.assertEqual(failures, [("unknown", "账号操作暂时失败，请稍后重试")])
        self.assertNotIn(secret, failures[0][1])
        self.assertFalse(self.paths.account_file.exists())

    def test_repeated_login_click_does_not_create_concurrent_request(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        client = FakeJmAccountClient()
        original_login = client.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=3)
            return original_login(username, password)

        client.login = slow_login
        controller = self.make_controller([client])

        first = controller.login("test-user", "test-password")
        self.assertTrue(started.wait(timeout=1))
        second = controller.login("test-user", "test-password")
        release.set()
        self.assertTrue(
            self.wait_until(
                lambda: controller.current_snapshot.status
                is AccountStatus.SIGNED_IN
            )
        )

        self.assertEqual(first, 1)
        self.assertIsNone(second)
        self.assertEqual(client.calls, [("login", "test-user")])

    def test_logout_invalidates_running_login_and_removes_late_file(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        client = FakeJmAccountClient()
        original_login = client.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=3)
            return original_login(username, password)

        client.login = slow_login
        controller = self.make_controller([client])
        snapshots = []
        controller.snapshot_changed.connect(snapshots.append)

        controller.login("test-user", "test-password")
        self.assertTrue(started.wait(timeout=1))
        logout_generation = controller.logout()
        self.assertEqual(logout_generation, 2)
        release.set()
        self.assertTrue(
            self.wait_until(
                lambda: not controller.is_busy
                and controller.current_snapshot.status
                is AccountStatus.SIGNED_OUT
            )
        )

        self.assertFalse(self.paths.account_file.exists())
        self.assertEqual(snapshots[-1].status, AccountStatus.SIGNED_OUT)

    def test_logout_delete_failure_reports_local_data_state(self):
        controller = self.make_controller()
        failures = []
        controller.operation_failed.connect(lambda *args: failures.append(args))
        controller.login("test-user", "test-password")
        self.assertTrue(
            self.wait_until(
                lambda: controller.current_snapshot.status
                is AccountStatus.SIGNED_IN
            )
        )

        with patch.object(
            self.favorites_store,
            "delete",
            side_effect=ProtectedStoreDeleteError("private disk details"),
        ):
            controller.logout()
            self.assertTrue(
                self.wait_until(
                    lambda: controller.current_snapshot.status
                    is AccountStatus.LOCAL_DATA_UNREADABLE
                    and bool(failures)
                )
            )

        self.assertEqual(failures[-1][0], "storage")
        self.assertNotIn("private", failures[-1][1])
        self.assertFalse(self.paths.account_file.exists())

    def test_dispose_is_nonblocking_and_suppresses_late_result(self):
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        client = FakeJmAccountClient()
        original_login = client.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=3)
            return original_login(username, password)

        client.login = slow_login
        controller = self.make_controller([client])
        snapshots = []
        controller.snapshot_changed.connect(snapshots.append)
        controller.login("test-user", "test-password")
        self.assertTrue(started.wait(timeout=1))

        before = time.perf_counter()
        controller.dispose()
        elapsed = time.perf_counter() - before
        release.set()
        self.process_for(80)

        self.assertLess(elapsed, 0.1)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].status, AccountStatus.SIGNING_IN)
        self.assertFalse(self.paths.account_file.exists())
        self.assertIsNone(controller.login("name", "password"))

    def test_dispose_preserves_confirmed_logout_cleanup(self):
        first = FakeJmAccountClient()
        second = FakeJmAccountClient()
        started = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        original_login = second.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=3)
            return original_login(username, password)

        second.login = slow_login
        controller = self.make_controller([first, second])
        controller.login("test-user", "test-password")
        self.assertTrue(
            self.wait_until(
                lambda: controller.current_snapshot.status
                is AccountStatus.SIGNED_IN
            )
        )
        self.assertTrue(self.paths.account_file.is_file())
        controller.mark_expired()
        controller.login("test-user", "test-password")
        self.assertTrue(started.wait(timeout=1))

        controller.logout()
        controller.dispose()
        release.set()
        deadline = time.monotonic() + 2
        while self.paths.account_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(self.paths.account_file.exists())

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

    def process_for(self, duration_ms):
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(duration_ms)
        loop.exec()


if __name__ == "__main__":
    unittest.main()
