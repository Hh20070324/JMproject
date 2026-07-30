from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from jm_downloader.account import build_account_client
from jm_downloader.models import TaskConfig
from jm_downloader.option_config import (
    API_ROUTE_LABELS,
    ApiRouteState,
    apply_api_route,
)
from jm_downloader.qt.controllers.settings_controller import (
    SettingsController,
)
from jm_downloader.qt.settings_store import SettingsStore
from jm_downloader.search import SearchService
from jm_downloader.settings import AppPaths, AppSettings


FIXED_ROUTE = "www.cdngwc.cc"


class V29ApiRouteTests(unittest.TestCase):
    def test_route_state_and_option_injection_are_explicit(self):
        state = ApiRouteState()
        option = SimpleNamespace(
            client=SimpleNamespace(domain=["automatic"])
        )

        self.assertEqual(state.get(), "auto")
        apply_api_route(option, "auto")
        self.assertEqual(option.client.domain, ["automatic"])

        state.set(FIXED_ROUTE)
        apply_api_route(option, state.get())
        self.assertEqual(option.client.domain, [FIXED_ROUTE])
        self.assertEqual(API_ROUTE_LABELS[FIXED_ROUTE], "路线 2")

    def test_download_worker_applies_task_bound_route_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            option = Mock()
            option.download.threading = SimpleNamespace(image=1, photo=1)
            option.client.retry_times = 0
            option.client.postman.meta_data.timeout = 0
            option.client.domain = []
            with patch(
                "jm_downloader.downloader.jmcomic.create_option_by_file",
                return_value=option,
            ):
                from jm_downloader.downloader import DownloadWorker

                worker = DownloadWorker(
                    "123",
                    paths=paths,
                    selected_chapter_ids=("1",),
                    task_config=TaskConfig(api_route=FIXED_ROUTE),
                )
                worker._make_option()

        self.assertEqual(option.client.domain, [FIXED_ROUTE])

    def test_account_client_fixed_route_does_not_use_global_domain_cache(self):
        option = Mock()
        option.client.domain = []
        option.new_jm_client.return_value = Mock(
            spec=["get_meta_data", "retry_times"]
        )
        option.new_jm_client.return_value.get_meta_data.return_value = 15
        with (
            patch(
                "jm_downloader.account.jmcomic.create_option_by_file",
                return_value=option,
            ),
            patch(
                "jm_downloader.account.jmcomic.JmApiClient",
                type(option.new_jm_client.return_value),
            ),
        ):
            build_account_client(Path("option.yml"), api_route=FIXED_ROUTE)

        self.assertEqual(option.client.domain, [FIXED_ROUTE])

    def test_search_rebuilds_thread_client_after_route_change(self):
        state = ApiRouteState("auto")
        created = []

        def factory():
            client = Mock()
            created.append(client)
            return client

        service = SearchService(
            client_factory=factory,
            api_route_provider=state.get,
        )

        first = service._get_client_for_operation("search")
        state.set(FIXED_ROUTE)
        second = service._get_client_for_operation("search")

        self.assertIsNot(first, second)
        self.assertEqual(len(created), 2)

    def test_user_route_probe_runs_off_main_thread_and_reports_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            store = SettingsStore(paths)
            store.settings = AppSettings(api_route=FIXED_ROUTE)
            called = []
            finished = threading.Event()

            def probe(path, route):
                called.append((path, route, threading.current_thread().name))
                finished.set()
                return 37

            controller = SettingsController(store, route_probe=probe)

            controller.test_api_route(FIXED_ROUTE)
            self.assertTrue(finished.wait(2))

        self.assertEqual(called[0][0], paths.option_file)
        self.assertEqual(called[0][1], FIXED_ROUTE)
        self.assertEqual(called[0][2], "api-route-probe")

    def test_route_probe_publishes_sanitized_success_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SettingsStore(AppPaths(Path(temp_dir)))
            store.settings = AppSettings()
            controller = SettingsController(
                store,
                route_probe=lambda _path, _route: 37,
            )
            results = []
            controller.route_test_succeeded.connect(
                lambda route, elapsed: results.append((route, elapsed))
            )
            with controller._route_test_lock:
                controller._route_test_generation = 1

            controller._run_route_test(1, FIXED_ROUTE)

        self.assertEqual(results, [(FIXED_ROUTE, 37)])
