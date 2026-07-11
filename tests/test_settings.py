import tempfile
import unittest
from pathlib import Path

from jm_downloader.settings import AppPaths


class AppPathsTests(unittest.TestCase):
    def test_keeps_user_data_beside_executable_and_web_assets_in_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            resources = Path(temp_dir) / "bundle"
            paths = AppPaths(root=root, resources=resources)

            self.assertEqual(paths.pictures, root / "Pictures")
            self.assertEqual(paths.pdfs, root / "PDFs")
            self.assertEqual(paths.option_file, root / "option.yml")
            self.assertEqual(paths.web, resources / "static")


if __name__ == "__main__":
    unittest.main()
