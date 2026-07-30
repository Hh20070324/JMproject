from pathlib import Path
import os
import tempfile
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import IcoImagePlugin, Image
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from jm_downloader.qt.theme import resource_path
from scripts.generate_app_icon import ICON_SIZES, generate


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = (
    PROJECT_ROOT
    / "jm_downloader"
    / "qt"
    / "resources"
    / "app.ico"
)


class V321ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v321-app-icon-tests"]
        )

    def test_controlled_ico_has_all_required_sizes_and_green_art(self):
        with ICON_PATH.open("rb") as stream:
            sizes = IcoImagePlugin.IcoFile(stream).sizes()
        self.assertEqual(
            sizes,
            {(size, size) for size in ICON_SIZES},
        )

        with Image.open(ICON_PATH) as image:
            image.size = (256, 256)
            rgba = image.convert("RGBA")
        pixels = tuple(rgba.get_flattened_data())
        visible = [
            pixel
            for pixel in pixels
            if pixel[3] >= 128
        ]
        self.assertTrue(visible)
        self.assertTrue(any(pixel[3] == 0 for pixel in pixels))
        red = sum(pixel[0] for pixel in visible) / len(visible)
        green = sum(pixel[1] for pixel in visible) / len(visible)
        blue = sum(pixel[2] for pixel in visible) / len(visible)
        self.assertGreater(green, red)
        self.assertGreater(green, blue)

    def test_generator_is_reproducible_without_temporary_image_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "generated.ico"
            generate(output)
            self.assertEqual(output.read_bytes(), ICON_PATH.read_bytes())
            self.assertEqual(
                tuple(path.name for path in output.parent.iterdir()),
                ("generated.ico",),
            )

    def test_runtime_resource_loads_as_a_nonempty_qicon(self):
        resolved = resource_path("app.ico")
        self.assertEqual(resolved.resolve(), ICON_PATH.resolve())
        icon = QIcon(str(resolved))
        self.assertFalse(icon.isNull())
        for size in ICON_SIZES:
            self.assertFalse(icon.pixmap(size, size).isNull())

    def test_both_executables_and_runtime_use_the_controlled_icon(self):
        spec = (PROJECT_ROOT / "JM-Downloader.spec").read_text(
            encoding="utf-8"
        )
        app_source = (
            PROJECT_ROOT / "jm_downloader" / "qt" / "app.py"
        ).read_text(encoding="utf-8")
        window_source = (
            PROJECT_ROOT / "jm_downloader" / "qt" / "main_window.py"
        ).read_text(encoding="utf-8")
        build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            spec.count('icon="jm_downloader/qt/resources/app.ico"'),
            2,
        )
        self.assertIn("jm_downloader/qt/resources/app.ico", spec)
        self.assertIn('resource_path("app.ico")', app_source)
        self.assertNotIn("SP_DriveHDIcon", app_source)
        self.assertNotIn("SP_DriveHDIcon", window_source)
        self.assertIn('Assert-BundledFile "app.ico"', build_script)


if __name__ == "__main__":
    unittest.main()
