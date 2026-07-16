import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from jm_downloader import downloader
from jm_downloader.jmcomic_client import serialized_client_construction
from jm_downloader.settings import AppPaths


class JmcomicClientConstructionTests(unittest.TestCase):
    def test_shared_scope_serializes_concurrent_construction(self):
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def build_client():
            nonlocal active, maximum_active
            with serialized_client_construction():
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.01)
                with state_lock:
                    active -= 1

        threads = [threading.Thread(target=build_client) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(maximum_active, 1)

    def test_download_info_releases_scope_before_network_request(self):
        events = []

        @contextmanager
        def construction_scope():
            events.append("enter")
            yield
            events.append("exit")

        client = Mock()
        client.get_album_detail.side_effect = lambda _album_id: (
            events.append("request")
            or Mock(title="Title", cover=None, page_count=1)
        )
        option = Mock()
        option.build_jm_client.side_effect = lambda: (
            events.append("build") or client
        )

        worker = object.__new__(downloader.DownloadWorker)
        worker.album_id = "1449491"
        worker.paths = AppPaths(Path("."))
        worker._make_option = Mock(return_value=option)

        with (
            patch.object(downloader, "install_safe_jmcomic_logging"),
            patch.object(
                downloader,
                "serialized_client_construction",
                construction_scope,
            ),
        ):
            result = worker.fetch_info()

        self.assertEqual(result, ("Title", None, 1))
        self.assertEqual(events, ["enter", "build", "exit", "request"])


if __name__ == "__main__":
    unittest.main()
