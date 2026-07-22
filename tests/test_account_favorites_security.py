from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import (
    FavoriteCacheStore,
    FavoritesAddUncertain,
    FavoritesMutationUncertain,
    FavoritesService,
    FavoritesUnavailable,
)
from jm_downloader.models import TaskStatus
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.settings import AppPaths, AppSettings
from jm_downloader.task_store import StoredTask, TaskStore
from tests.account_fakes import FakeJmAccountClient


FIXED_TIME = datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)


class SecurityProtector:
    PREFIX = b"security-acceptance\0"

    def protect(self, plaintext: bytes) -> bytes:
        return self.PREFIX + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.PREFIX):
            raise ValueError("invalid ciphertext")
        return ciphertext[len(self.PREFIX) :][::-1]


class AccountFavoritesSecurityAcceptanceTests(unittest.TestCase):
    def test_runtime_files_and_logs_do_not_contain_account_secrets(self):
        username = "security-user-sentinel"
        password = "security-password-sentinel"
        cookie = "security-cookie-sentinel"
        endpoint = "security-endpoint-sentinel"
        title = "security-title-sentinel"
        folder_name = "security-folder-sentinel"

        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths(Path(directory))
            paths.logs.mkdir()
            log_file = paths.logs / "app.log"
            handler = logging.FileHandler(
                log_file,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
            logger = logging.getLogger("jm-downloader")
            old_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)
            task_store = TaskStore(paths)
            try:
                protector = SecurityProtector()
                account_protected = ProtectedStore.account(paths, protector)
                favorites_protected = ProtectedStore.favorites(paths, protector)
                client = FakeJmAccountClient(
                    username=username,
                    password=password,
                    cookies={"session": cookie},
                    folders={
                        "0": (
                            "Default",
                            (("1449491", {"name": title}),),
                        ),
                        "8": ("Reading", ()),
                        "9": ("Disposable", ()),
                    },
                )
                account_service = AccountService(
                    paths,
                    account_store=AccountStore(account_protected),
                    favorites_store=favorites_protected,
                    client_factory=lambda _cookies: client,
                    clock=lambda: FIXED_TIME,
                )
                account_service.login(
                    username,
                    password,
                    account_service.start_operation(),
                )
                favorites_service = FavoritesService(
                    account_service,
                    paths,
                    cache_store=FavoriteCacheStore(favorites_protected),
                    client_factory=lambda _cookies: client,
                    clock=lambda: FIXED_TIME,
                )
                favorites_service.sync(favorites_service.start_operation())
                favorites_before_add = paths.favorites_file.read_bytes()
                sync_timestamp = favorites_service.snapshot.synced_at_utc

                favorites_service.add_album(
                    "350234",
                    favorites_service.start_operation(),
                )
                self.assertEqual(
                    paths.favorites_file.read_bytes(),
                    favorites_before_add,
                )
                self.assertEqual(
                    favorites_service.snapshot.synced_at_utc,
                    sync_timestamp,
                )

                client.favorite_add_error = ConnectionResetError(endpoint)
                with self.assertRaises(FavoritesAddUncertain) as add_error:
                    favorites_service.add_album(
                        "350235",
                        favorites_service.start_operation(),
                    )
                self.assertNotIn(endpoint, str(add_error.exception))
                self.assertEqual(
                    paths.favorites_file.read_bytes(),
                    favorites_before_add,
                )
                self.assertEqual(
                    [
                        call
                        for call in client.calls
                        if call[0] == "add_favorite_album"
                    ],
                    [
                        ("add_favorite_album", "350234", "0"),
                        ("add_favorite_album", "350235", "0"),
                    ],
                )
                self.assertEqual(client.retry_times, 0)

                client.favorite_folder_mutation_errors["move"] = (
                    ConnectionResetError(endpoint)
                )
                with self.assertRaises(FavoritesMutationUncertain) as move_error:
                    favorites_service.move_album(
                        "1449491",
                        "8",
                        favorites_service.start_operation(),
                    )
                self.assertNotIn(endpoint, str(move_error.exception))
                client.favorite_folder_mutation_errors.pop("move")

                favorites_service.create_folder(
                    folder_name,
                    favorites_service.start_operation(),
                )
                favorites_service.move_album(
                    "1449491",
                    "8",
                    favorites_service.start_operation(),
                )
                favorites_service.delete_folder(
                    "9",
                    favorites_service.start_operation(),
                )
                self.assertEqual(
                    paths.favorites_file.read_bytes(),
                    favorites_before_add,
                )

                client.favorite_errors[("0", 1)] = TimeoutError(endpoint)
                with self.assertRaises(FavoritesUnavailable) as raised:
                    favorites_service.sync(
                        favorites_service.start_operation()
                    )
                self.assertNotIn(endpoint, str(raised.exception))

                SettingsStore(paths).save(AppSettings())
                task_store.save(
                    (
                        StoredTask(
                            id="security-task",
                            album_id="1449491",
                            title="Local task",
                            status=TaskStatus.PAUSED,
                            progress=30,
                            chapter="",
                            page="",
                            error=None,
                            pictures_directory="Pictures",
                            pdf_directory="PDFs",
                        ),
                    )
                )
                self.assertTrue(task_store.flush(timeout=2))
                handler.flush()

                runtime_files = (
                    paths.account_file,
                    paths.favorites_file,
                    paths.settings_file,
                    paths.tasks_file,
                    log_file,
                )
                secrets = tuple(
                    value.encode("utf-8")
                    for value in (
                        username,
                        password,
                        cookie,
                        endpoint,
                        title,
                        folder_name,
                    )
                )
                for path in runtime_files:
                    with self.subTest(path=path.name):
                        raw = path.read_bytes()
                        for secret in secrets:
                            self.assertNotIn(secret, raw)
            finally:
                task_store.close(timeout=2)
                logger.removeHandler(handler)
                logger.setLevel(old_level)
                handler.close()


if __name__ == "__main__":
    unittest.main()
