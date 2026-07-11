import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from desktop import DesktopServer
from jm_downloader.application import create_app
from jm_downloader.library import LibraryService
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskManager


class DesktopServerTests(unittest.TestCase):
    def test_uses_available_port_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            paths.web.mkdir()
            (paths.web / "index.html").write_text("desktop test", encoding="utf-8")
            manager = TaskManager(paths=paths)
            library = LibraryService(paths=paths)

            server = DesktopServer(
                application=create_app(
                    paths=paths,
                    manager=manager,
                    library=library,
                )
            )

            server.start()
            try:
                with urlopen(server.url, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"desktop test")
                self.assertNotEqual(server._server.server_port, 0)
            finally:
                server.stop()

            self.assertFalse(server._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
