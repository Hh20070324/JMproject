import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.favorites import FavoriteCacheStore, FavoritesService
from jm_downloader.models import (
    AccountStatus,
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.controllers.favorites_controller import (
    FavoritesController,
)
from jm_downloader.qt.controllers.search_controller import SearchController
from jm_downloader.search import SearchService, SearchUnavailable
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient
from tests.test_favorites import TestProtector
from tests.test_v321_phase2_async_search import (
    AsyncClient,
    SyncClient,
)
from tests.test_v321_phase3_async_favorites import AsyncFavoritesClient
from tests import test_v321_phase4_silent_reauth as silent_reauth_tests


FIXED_TIME = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CapturingSearchService:
    def __init__(self, engine):
        self.engine = engine
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def capture_query_engine(self):
        return self.engine

    def search(self, request, *, query_engine):
        self.calls.append(query_engine)
        self.started.set()
        self.release.wait(timeout=2)
        return SearchPageSnapshot(
            request,
            1,
            1,
            (SearchResultSnapshot("123", "Example"),),
        )


class BlockingAsyncFavoritesClient(AsyncFavoritesClient):
    def __init__(self, inner):
        super().__init__(inner)
        self.request_started = threading.Event()
        self.release = threading.Event()

    async def favorite_folder(self, **kwargs):
        self.request_started.set()
        await asyncio.to_thread(self.release.wait, 2)
        return await super().favorite_folder(**kwargs)


class V321EngineAndCancellationReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v321-engine-reliability-tests"]
        )

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
        self.account = AccountService(
            self.paths,
            account_store=self.account_store,
            favorites_store=self.favorites_store,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        self.account.login(
            "test-user",
            "test-password",
            self.account.start_operation(),
        )
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.dispose()
            for worker_name in ("_worker", "_filter_worker"):
                worker = getattr(controller, worker_name, None)
                if worker is not None:
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
            for worker in getattr(controller, "_workers", ()):
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def wait_until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    def test_repeated_async_sync_switches_close_every_async_client(self):
        search_async_clients = []
        search_sync_clients = []

        def new_search_async(_route):
            client = AsyncClient()
            search_async_clients.append(client)
            return client

        def new_search_sync():
            client = SyncClient()
            search_sync_clients.append(client)
            return client

        search = SearchService(
            client_factory=new_search_sync,
            async_client_factory=new_search_async,
        )
        request = SearchRequest(SearchMode.GENERAL, "loop")
        for index in range(24):
            engine = "async" if index % 2 == 0 else "sync"
            snapshot = search.search(request, query_engine=engine)
            self.assertEqual(snapshot.request, request)

        favorites_async_clients = []
        favorites_sync_clients = []

        def new_favorites_async(_cookies, _route):
            client = AsyncFavoritesClient(FakeJmAccountClient())
            favorites_async_clients.append(client)
            return client

        def new_favorites_sync(_cookies):
            client = FakeJmAccountClient()
            favorites_sync_clients.append(client)
            return client

        favorites = FavoritesService(
            self.account,
            self.paths,
            cache_store=FavoriteCacheStore(self.favorites_store),
            client_factory=new_favorites_sync,
            async_client_factory=new_favorites_async,
            clock=lambda: FIXED_TIME,
        )
        for index in range(16):
            engine = "async" if index % 2 == 0 else "sync"
            snapshot = favorites.sync(
                favorites.start_operation(),
                query_engine=engine,
            )
            self.assertTrue(snapshot.folders)

        self.assertEqual(len(search_async_clients), 12)
        self.assertEqual(len(search_sync_clients), 1)
        self.assertEqual(
            len(search_sync_clients[0].calls),
            12,
        )
        self.assertTrue(
            all(client.close_called == 1 for client in search_async_clients)
        )
        self.assertEqual(len(favorites_async_clients), 8)
        self.assertEqual(len(favorites_sync_clients), 8)
        self.assertTrue(
            all(
                client.close_called == 1
                for client in favorites_async_clients
            )
        )

    def test_search_job_keeps_submission_engine_after_setting_changes(self):
        service = CapturingSearchService("async")
        controller = SearchController(
            service,
            worker_count=1,
            result_interval_ms=2,
        )
        self.controllers.append(controller)

        controller.search(SearchMode.GENERAL, "snapshot")
        service.engine = "sync"
        self.assertTrue(service.started.wait(timeout=1))
        service.release.set()
        self.assertTrue(self.wait_until(lambda: not controller.is_busy))

        self.assertEqual(service.calls, ["async"])

    def test_favorites_job_keeps_submission_engine_after_setting_changes(self):
        selected = ["async"]
        active = AsyncFavoritesClient(FakeJmAccountClient())
        sync_created = []
        service = FavoritesService(
            self.account,
            self.paths,
            cache_store=FavoriteCacheStore(self.favorites_store),
            client_factory=lambda _cookies: sync_created.append(True),
            query_engine_provider=lambda: selected[0],
            async_client_factory=lambda _cookies, _route: active,
            clock=lambda: FIXED_TIME,
        )
        account_controller = AccountController(
            self.account,
            result_interval_ms=2,
            auto_restore=False,
        )
        favorites_controller = FavoritesController(
            service,
            account_controller,
            result_interval_ms=2,
        )
        self.controllers.extend(
            [favorites_controller, account_controller]
        )
        self.assertTrue(
            self.wait_until(lambda: not favorites_controller.is_busy)
        )

        favorites_controller.sync()
        selected[0] = "sync"

        self.assertTrue(
            self.wait_until(
                lambda: (
                    not favorites_controller.is_busy
                    and active.close_called == 1
                )
            )
        )
        self.assertEqual(sync_created, [])
        self.assertTrue(
            any(
                call[0] == "favorite_folder"
                for call in active.inner.calls
            )
        )

    def test_async_favorites_cancel_closes_client_and_keeps_old_snapshot(self):
        cache = FavoriteCacheStore(self.favorites_store)
        initial_service = FavoritesService(
            self.account,
            self.paths,
            cache_store=cache,
            client_factory=lambda _cookies: FakeJmAccountClient(),
            clock=lambda: FIXED_TIME,
        )
        old_snapshot = initial_service.sync(
            initial_service.start_operation(),
            query_engine="sync",
        )
        active = BlockingAsyncFavoritesClient(FakeJmAccountClient())
        service = FavoritesService(
            self.account,
            self.paths,
            cache_store=cache,
            query_engine_provider=lambda: "async",
            async_client_factory=lambda _cookies, _route: active,
            clock=lambda: FIXED_TIME,
        )
        account_controller = AccountController(
            self.account,
            result_interval_ms=2,
            auto_restore=False,
        )
        favorites_controller = FavoritesController(
            service,
            account_controller,
            result_interval_ms=2,
        )
        self.controllers.extend(
            [favorites_controller, account_controller]
        )
        self.assertTrue(
            self.wait_until(
                lambda: (
                    favorites_controller.current_snapshot == old_snapshot
                    and not favorites_controller.is_busy
                )
            )
        )
        failures = []
        favorites_controller.operation_failed.connect(
            lambda *args: failures.append(args)
        )

        favorites_controller.sync()
        self.assertTrue(active.request_started.wait(timeout=1))
        favorites_controller.cancel_sync()
        active.release.set()

        self.assertTrue(
            self.wait_until(lambda: active.close_called == 1)
        )
        self.assertEqual(
            favorites_controller.current_snapshot,
            old_snapshot,
        )
        self.assertEqual(failures[-1][0], "cancelled")

    def test_sensitive_error_details_never_enter_query_logs(self):
        secret = (
            "https://private.invalid/path?"
            "cookie=AVS-secret&password=secret-password"
        )
        search_client = AsyncClient(request_error=TimeoutError(secret))
        with self.assertLogs("jm-downloader", level="WARNING") as captured:
            with self.assertRaises(SearchUnavailable):
                SearchService(
                    async_client_factory=lambda _route: search_client
                ).search(
                    SearchRequest(SearchMode.GENERAL, "query"),
                    query_engine="async",
                )
        rendered = "\n".join(captured.output)
        for forbidden in (
            "private.invalid",
            "AVS-secret",
            "secret-password",
        ):
            self.assertNotIn(forbidden, rendered)


class V321SilentRecoveryBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        silent_reauth_tests.V321SilentReauthenticationTests.setUpClass()

    def setUp(self):
        self.harness = (
            silent_reauth_tests.V321SilentReauthenticationTests(
                methodName="runTest"
            )
        )
        self.harness.setUp()

    def tearDown(self):
        self.harness.tearDown()

    def slow_login_client(self):
        client = FakeJmAccountClient()
        started = threading.Event()
        release = threading.Event()
        original = client.login

        def slow_login(username, password):
            started.set()
            release.wait(timeout=2)
            return original(username, password)

        client.login = slow_login
        return client, started, release

    def start_blocked_recovery(self):
        login_client, started, release = self.slow_login_client()
        favorites, account, _ = self.harness.make_controllers(
            [
                self.harness.expired_client(),
                FakeJmAccountClient(),
            ],
            login_client=login_client,
        )
        favorites.sync()
        self.assertTrue(
            self.harness.wait_until(started.is_set)
        )
        return favorites, account, login_client, release

    def test_login_failure_stops_in_expired_manual_flow(self):
        login_client = FakeJmAccountClient()
        login_client.login_error = TimeoutError("private endpoint")
        favorites, account, _ = self.harness.make_controllers(
            [self.harness.expired_client()],
            login_client=login_client,
        )

        favorites.sync()

        self.assertTrue(
            self.harness.wait_until(
                lambda: (
                    not favorites.is_busy
                    and not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                )
            )
        )
        self.assertEqual(login_client.calls, [("login", "test-user")])
        self.assertIsNone(favorites._read_recovery)

    def test_logout_cancels_blocked_recovery_and_deletes_credentials(self):
        favorites, account, login_client, release = (
            self.start_blocked_recovery()
        )

        account.logout()
        release.set()

        self.assertTrue(
            self.harness.wait_until(
                lambda: (
                    not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.SIGNED_OUT
                )
            )
        )
        self.assertFalse(self.harness.paths.credentials_file.exists())
        self.assertIsNone(favorites._read_recovery)
        self.assertEqual(login_client.calls, [("login", "test-user")])

    def test_disabling_remember_cancels_blocked_recovery(self):
        favorites, account, login_client, release = (
            self.start_blocked_recovery()
        )

        account.set_remember_credentials(False)
        release.set()

        self.assertTrue(
            self.harness.wait_until(
                lambda: (
                    not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                )
            )
        )
        self.assertFalse(self.harness.paths.credentials_file.exists())
        self.assertIsNone(favorites._read_recovery)
        self.assertEqual(login_client.calls, [("login", "test-user")])

    def test_user_cancel_stops_blocked_recovery_and_read_retry(self):
        favorites, account, login_client, release = (
            self.start_blocked_recovery()
        )

        favorites.cancel_sync()
        release.set()

        self.assertTrue(
            self.harness.wait_until(
                lambda: (
                    not account.is_busy
                    and account.current_snapshot.status
                    is AccountStatus.EXPIRED
                )
            )
        )
        self.assertIsNone(favorites._read_recovery)
        self.assertEqual(login_client.calls, [("login", "test-user")])

    def test_dispose_is_nonblocking_and_suppresses_recovery_retry(self):
        favorites, account, login_client, release = (
            self.start_blocked_recovery()
        )

        started = time.perf_counter()
        favorites.dispose()
        account.dispose()
        elapsed = time.perf_counter() - started
        release.set()

        self.assertLess(elapsed, 0.1)
        self.assertTrue(
            self.harness.wait_until(lambda: bool(login_client.calls))
        )
        self.assertIsNone(favorites._read_recovery)
        self.assertEqual(login_client.calls, [("login", "test-user")])


class V321ScaleAndReleaseReliabilityTests(unittest.TestCase):
    def test_library_card_geometry_in_two_themes_and_four_scales(self):
        code = r"""
import os
from pathlib import Path
import tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from jm_downloader.models import LibraryItem, LibraryLayout
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.library_item_card import LibraryItemCard

app = QApplication(["v321-scale-audit"])
ThemeManager(os.environ["JM_TEST_THEME"]).apply()
with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    folder = root / "folder"
    folder.mkdir()
    item = LibraryItem(
        "123", "缩放测试漫画", LibraryLayout.MANAGED,
        2, 20, 2048, None, folder, 1024, folder, 1024,
    )
    card = LibraryItemCard(item)
    card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    card.resize(card.minimumSizeHint().width(), 164)
    card.show()
    app.processEvents()
    assert card.height() == 164
    secondary = (
        card.view_task_button,
        card.chapter_button,
        card.delete_button,
    )
    primary = (
        card.open_images_button,
        card.open_pdf_button,
        card.read_button,
    )
    assert all(button.isVisible() for button in (*secondary, *primary))
    for group in (secondary, primary):
        for index, button in enumerate(group):
            assert button.geometry().width() > 0
            assert button.geometry().height() > 0
            for other in group[index + 1:]:
                assert not button.geometry().intersects(other.geometry())
    left_edge = secondary[-1].mapTo(card, secondary[-1].rect().topRight()).x()
    right_edge = primary[0].mapTo(card, primary[0].rect().topLeft()).x()
    assert left_edge < right_edge
    image = card.grab().toImage()
    assert not image.isNull()
"""
        for theme in ("light", "dark"):
            for scale in ("1", "1.25", "1.5", "2"):
                with self.subTest(theme=theme, scale=scale):
                    env = os.environ.copy()
                    env["QT_QPA_PLATFORM"] = "offscreen"
                    env["QT_SCALE_FACTOR"] = scale
                    env["JM_TEST_THEME"] = theme
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=PROJECT_ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )

    def test_release_keeps_icon_notice_and_excludes_generated_runtime_data(self):
        notices = (
            PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"
        ).read_text(encoding="utf-8")
        spec = (PROJECT_ROOT / "JM-Downloader.spec").read_text(
            encoding="utf-8"
        )
        build = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("Game-Icon-Pack", notices)
        self.assertIn("CC0", notices)
        self.assertIn("app.ico", spec)
        self.assertIn('Assert-BundledFile "app.ico"', build)
        for forbidden in (
            "credentials.dat",
            "favorites.dat",
            "search_history.dat",
            "reading_history.dat",
        ):
            self.assertIn(forbidden, build)


if __name__ == "__main__":
    unittest.main()
