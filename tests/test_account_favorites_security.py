from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import (
    FavoriteCacheStore,
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
                        )
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
                    for value in (username, password, cookie, endpoint, title)
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
