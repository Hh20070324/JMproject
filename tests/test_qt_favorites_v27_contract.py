from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import FavoriteCacheStore, FavoritesService
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.controllers.favorites_controller import FavoritesController
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


class ControllerContractProtector:
    PREFIX = b"favorites-v27-controller\0"

    def protect(self, plaintext):
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid")
        return ciphertext[len(self.PREFIX) :][::-1]


class FavoritesV27ControllerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["favorites-v27-controller-contract"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = ControllerContractProtector()
        account_protected = ProtectedStore.account(self.paths, protector)
        favorites_protected = ProtectedStore.favorites(self.paths, protector)
        login_service = AccountService(
            self.paths,
            account_store=AccountStore(account_protected),
            favorites_store=favorites_protected,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        login_service.login(
            "test-user",
            "test-password",
            login_service.start_operation(),
        )
        self.account_service = AccountService(
            self.paths,
            account_store=AccountStore(account_protected),
            favorites_store=favorites_protected,
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
                "0": ("Default", (("1", {"name": "One"}),)),
                "8": ("Reading", ()),
            }
        )
        self.favorites_service = FavoritesService(
            self.account_service,
            self.paths,
            cache_store=FavoriteCacheStore(favorites_protected),
            client_factory=lambda _cookies: self.client,
            clock=lambda: FIXED_TIME,
        )
        self.controller = FavoritesController(
            self.favorites_service,
            self.account_controller,
            result_interval_ms=2,
        )
        self.assertTrue(
            self.wait_until(lambda: self.controller.current_snapshot is not None)
        )
        self.assertIsNotNone(self.controller.sync())
        self.assertTrue(
            self.wait_until(
                lambda: self.controller.current_snapshot is not None
                and self.controller.current_snapshot.synced_at_utc is not None
                and not self.controller.is_busy
            )
        )
        self.client.calls.clear()

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

    def test_custom_target_add_runs_add_move_then_full_sync(self):
        successes = []
        self.controller.add_succeeded.connect(successes.append)

        generation = self.controller.add_album("777777", "8")

        self.assertIsNotNone(generation)
        self.assertTrue(self.wait_until(lambda: successes == ["777777"]))
        calls = self.client.calls
        self.assertEqual(calls[0], ("add_favorite_album", "777777", "0"))
        self.assertEqual(
            calls[1],
            (
                "favorite_folder_mutation",
                "move",
                {"type": "move", "aid": "777777", "folder_id": "8"},
            ),
        )
        self.assertTrue(
            any(call[0] == "favorite_folder" for call in calls[2:])
        )
        self.assertTrue(self.paths.favorites_file.is_file())
        self.assertIn("777777", self.controller.known_favorite_ids)

    def test_move_failure_after_add_reports_partial_success_and_syncs(self):
        self.client.favorite_folder_mutation_errors["move"] = TimeoutError(
            "private endpoint"
        )
        partial = []
        successes = []
        failures = []
        self.controller.add_partially_succeeded.connect(
            lambda *args: partial.append(args)
        )
        self.controller.add_succeeded.connect(successes.append)
        self.controller.add_failed.connect(lambda *args: failures.append(args))

        generation = self.controller.add_album("777777", "8")

        self.assertIsNotNone(generation)
        self.assertTrue(self.wait_until(lambda: bool(partial)))
        self.assertEqual(successes, [])
        self.assertEqual(failures, [])
        self.assertEqual(partial[0][0], "777777")
        self.assertEqual(partial[0][1], "mutation_uncertain")
        self.assertIn("已收藏", partial[0][-1])
        self.assertIn("结果无法确认", partial[0][-1])
        self.assertTrue(
            any(call[0] == "favorite_folder" for call in self.client.calls[2:])
        )
        self.assertIn("777777", self.controller.known_favorite_ids)

    def test_move_expiry_after_add_keeps_the_specific_partial_reason(self):
        self.client.favorite_folder_mutation_errors["move"] = PermissionError(
            "private response"
        )
        partial = []
        self.controller.add_partially_succeeded.connect(
            lambda *args: partial.append(args)
        )

        generation = self.controller.add_album("777777", "8")

        self.assertIsNotNone(generation)
        self.assertTrue(self.wait_until(lambda: bool(partial)))
        self.assertEqual(partial[0][0:2], ("777777", "session_expired"))
        self.assertIn("登录会话已过期", partial[0][2])

    def test_confirmed_folder_writes_automatically_refresh_snapshot(self):
        generation = self.controller.create_folder("Later")

        self.assertIsNotNone(generation)
        self.assertTrue(
            self.wait_until(
                lambda: not self.controller.is_busy
                and self.controller.current_snapshot is not None
                and any(
                    folder.name == "Later"
                    for folder in self.controller.current_snapshot.folders
                )
            )
        )
        self.assertEqual(
            self.client.calls[0][0:2],
            ("favorite_folder_mutation", "add"),
        )
        self.assertTrue(
            any(call[0] == "favorite_folder" for call in self.client.calls[1:])
        )


if __name__ == "__main__":
    unittest.main()
