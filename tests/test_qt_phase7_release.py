import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_NAME = "JM-Downloader-v2.5.0-Windows-x64.zip"
RUNTIME_LICENSE_ASSERTIONS = {
    "Game-Icon-Pack-CC0-1.0.txt": "CC0 1.0 Universal",
    "JMComic-Crawler-Python-2.7.1.txt": "Copyright (c) 2023 hect0x7",
    "commonX-0.6.40.txt": "Copyright (c) 2023 hect0x7",
    "curl_cffi-0.15.0.txt": "Copyright (c) 2022 curl_cffi developers",
    "curl_cffi-0.15.0-native.txt": "libcurl/8.15.0-IMPERSONATE",
    "certifi-2026.6.17.txt": "Mozilla Public License",
    "cffi-2.0.0.txt": "MIT No Attribution",
    "Pillow-12.2.0.txt": "===== libavif-1.4.1 =====",
    "pycparser-3.0.txt": "Copyright (c) 2008-2022, Eli Bendersky",
    "PyCryptodome-3.23.0.txt": "BSD 2-Clause license",
    "PyInstaller-6.21.0.txt": "Bootloader Exception",
    "PyYAML-6.0.3.txt": "Copyright (c) 2017-2021 Ingy",
    "typing_extensions-4.16.0.txt": "A. HISTORY OF THE SOFTWARE",
}


class PhaseSevenReleaseTests(unittest.TestCase):
    def test_version_resources_match_the_release(self):
        formal = (PROJECT_ROOT / "version_info.txt").read_text(encoding="utf-8")
        debug = (PROJECT_ROOT / "version_info_debug.txt").read_text(
            encoding="utf-8"
        )
        spec = (PROJECT_ROOT / "JM-Downloader.spec").read_text(encoding="utf-8")

        for resource in (formal, debug):
            self.assertIn("filevers=(2, 5, 0, 0)", resource)
            self.assertIn("prodvers=(2, 5, 0, 0)", resource)
            self.assertIn("StringStruct('FileVersion', '2.5.0')", resource)
            self.assertIn("StringStruct('ProductVersion', '2.5.0')", resource)
            self.assertNotIn("2.1.0", resource)

        self.assertIn("OriginalFilename', 'JM-Downloader.exe'", formal)
        self.assertIn("OriginalFilename', 'JM-Downloader-Debug.exe'", debug)
        self.assertIn('version="version_info.txt"', spec)
        self.assertIn('version="version_info_debug.txt"', spec)
        self.assertIn("resources/icons/*.svg", spec)

    def test_release_name_and_checksum_are_consistent(self):
        build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        guide = (PROJECT_ROOT / "用户指南.md").read_text(encoding="utf-8")

        for document in (readme, guide):
            self.assertIn(ARCHIVE_NAME, document)
        self.assertIn('$ReleaseVersion = "2.5.0"', build_script)
        self.assertIn(
            '"JM-Downloader-v$ReleaseVersion-Windows-x64.zip"',
            build_script,
        )
        self.assertIn('ChecksumFile = "$Archive.sha256"', build_script)
        self.assertIn("Assert-ArchiveContents", build_script)
        self.assertNotIn('Remove-BuildDirectory $ReleaseDir', build_script)
        self.assertIn("JM-Downloader-v2.1.0-Windows-x64.zip", build_script)
        self.assertIn("JM-Downloader-v2.2.0-Windows-x64.zip", build_script)
        self.assertIn("JM-Downloader-v2.3.0-Windows-x64.zip", build_script)
        self.assertIn("JM-Downloader-v2.4.0-Windows-x64.zip", build_script)
        self.assertNotIn("`release/JM-Downloader-Windows-x64.zip`", readme)
        self.assertNotIn("`release/JM-Downloader-Windows-x64.zip`", guide)

    def test_runtime_state_is_excluded_from_release(self):
        build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        for runtime_name in (
            "tasks.json",
            "tasks.json.corrupt-*",
            ".tasks.json.*.tmp",
            "account.dat",
            "favorites.dat",
            ".account.dat.*.tmp",
            ".favorites.dat.*.tmp",
            "*.jm-part-*",
        ):
            self.assertIn(runtime_name, build_script)
            self.assertIn(runtime_name, gitignore)
        self.assertIn('$_ -match "^JM-Downloader/tasks\\.json$"', build_script)
        self.assertIn(
            '$_ -match "^JM-Downloader/(?:account|favorites)\\.dat$"',
            build_script,
        )
        self.assertIn('$_ -match "\\.jm-part-[^/]*$"', build_script)
        self.assertIn("Assert-NoSensitiveTestData", build_script)

    def test_qt_lgpl_materials_are_complete_and_referenced(self):
        lgpl = (PROJECT_ROOT / "LICENSES" / "LGPL-3.0-only.txt").read_text(
            encoding="utf-8"
        )
        gpl = (PROJECT_ROOT / "LICENSES" / "GPL-3.0-only.txt").read_text(
            encoding="utf-8"
        )
        notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )
        source = (PROJECT_ROOT / "QT_SOURCE_AND_RELINKING.md").read_text(
            encoding="utf-8"
        )
        qt_notices = (PROJECT_ROOT / "QT_THIRD_PARTY_NOTICES.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", lgpl)
        self.assertIn("Version 3, 29 June 2007", lgpl)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", gpl)
        self.assertIn("Version 3, 29 June 2007", gpl)

        for name in ("Qt 6.11.1", "PySide6 Essentials 6.11.1", "Shiboken6 6.11.1"):
            self.assertIn(name, notices)
        self.assertIn("GNU Lesser General Public License version 3 only", notices)
        normalized_notices = " ".join(notices.split())
        self.assertIn("dynamically loaded DLL and PYD", normalized_notices)

        self.assertIn("qt-everywhere-src-6.11.1.tar.xz", source)
        self.assertIn(
            "252acef8c5ae68074d91cadba2ee4a83465051bbb970dd26e8f0daa0f3904e03",
            source,
        )
        self.assertIn("pyside-setup-everywhere-src-6.11.1.tar.xz", source)
        self.assertIn(
            "6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2",
            source,
        )
        self.assertIn(
            "does not restrict reverse engineering",
            " ".join(source.split()),
        )
        self.assertIn("Data Compression Library (zlib)", qt_notices)
        self.assertIn("XSVG", qt_notices)
        self.assertIn("Python material adapted by Shiboken6", qt_notices)
        normalized_qt_notices = " ".join(qt_notices.split())
        for required_qt_notice in (
            "TIFF Software Distribution (libtiff)",
            "WebP (libwebp)",
            "UNICODE LICENSE V3",
            (
                "The text and information contained in this file may be "
                "freely used,"
            ),
            (
                "this software is based in part on the work of the "
                "Independent JPEG Group"
            ),
        ):
            self.assertIn(required_qt_notice, normalized_qt_notices)

    def test_bundled_python_license_inventory_is_complete(self):
        licenses_dir = PROJECT_ROOT / "LICENSES"
        index = (licenses_dir / "README.md").read_text(encoding="utf-8")
        notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )
        build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )
        spec = (PROJECT_ROOT / "JM-Downloader.spec").read_text(
            encoding="utf-8"
        )
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )

        for filename, marker in RUNTIME_LICENSE_ASSERTIONS.items():
            with self.subTest(license_file=filename):
                path = licenses_dir / filename
                self.assertTrue(path.is_file(), filename)
                content = path.read_text(encoding="utf-8")
                self.assertIn(marker, content)
                self.assertIn(filename, index)
                self.assertIn(filename, build_script)

        for component in (
            "commonX | 0.6.40",
            "curl_cffi | 0.15.0",
            "certifi | 2026.6.17",
            "cffi | 2.0.0",
            "Pillow | 12.2.0",
            "pycparser | 3.0",
            "PyCryptodome | 3.23.0",
            "PyYAML | 6.0.3",
            "typing_extensions | 4.16.0",
            "PyInstaller 6.21.0",
            "Game Icon Pack",
        ):
            self.assertIn(component, notices)

        pillow_license = (
            licenses_dir / "Pillow-12.2.0.txt"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(len(pillow_license.splitlines()), 1617)
        for native_component in (
            "===== harfbuzz-13.2.1 =====",
            "===== lcms2-2.18 =====",
            "===== libavif-1.4.1 =====",
            "===== libjpeg-turbo-3.1.4.1 =====",
        ):
            self.assertIn(native_component, pillow_license)

        native_notices = (
            licenses_dir / "curl_cffi-0.15.0-native.txt"
        ).read_text(encoding="utf-8")
        for native_component in (
            "curl 8.15.0",
            "curl-impersonate",
            "BoringSSL",
            "zlib 1.3",
            "Brotli 1.1.0",
            "zstd 1.5.6",
            "nghttp2 1.63.0",
            "ngtcp2 1.20.0",
            "nghttp3 1.15.0",
            "HTTPX 0.23.1",
        ):
            self.assertIn(native_component, native_notices)

        self.assertNotIn('collect_all("curl_cffi")', spec)
        self.assertIn('"packaging"', spec)
        self.assertIn('"setuptools"', spec)
        for requirement in (
            "jmcomic==2.7.1",
            "commonX==0.6.40",
            "curl-cffi==0.15.0",
            "cffi==2.0.0",
            "certifi==2026.6.17",
            "pycryptodome==3.23.0",
            "PyYAML==6.0.3",
            "pycparser==3.0",
            "typing_extensions==4.16.0",
            "Pillow==12.2.0",
            "PySide6-Essentials==6.11.1",
        ):
            self.assertIn(requirement, requirements)

    def test_four_scale_factors_render_a_nonblank_window(self):
        script = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import tempfile

            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtWidgets import QApplication

            from jm_downloader.models import (
                AccountSnapshot,
                AccountStatus,
                FavoriteFolderSnapshot,
                FavoriteItemSnapshot,
                FavoritesSnapshot,
                TaskSnapshot,
                TaskStatus,
            )
            from jm_downloader.qt.controllers.settings_controller import SettingsController
            from jm_downloader.qt.main_window import MainWindow
            from jm_downloader.qt.settings_store import SettingsStore
            from jm_downloader.qt.theme import ThemeManager
            from jm_downloader.qt.widgets.task_row import DownloadTaskRow
            from jm_downloader.settings import AppPaths

            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
            app = QApplication(["phase-seven-dpi-audit"])
            with tempfile.TemporaryDirectory() as temp_dir:
                controller = SettingsController(SettingsStore(AppPaths(Path(temp_dir))))
                theme = ThemeManager(controller.settings.theme)
                theme.apply()
                window = MainWindow(
                    theme,
                    settings_controller=controller,
                    persist_window_state=False,
                )
                window.resize(760, 520)
                window.show()
                app.processEvents()
                download_page = window.page("downloads")
                task_row = DownloadTaskRow(
                    TaskSnapshot(
                        id="scale-task",
                        album_id="123456",
                        title="用于验证高缩放布局的长标题",
                        status=TaskStatus.PAUSED,
                        progress=42,
                        chapter="第一章",
                        page="4/10",
                        preview_path=Path(temp_dir) / "preview.jpg",
                        preview_revision=1,
                        pdf_path=None,
                        error=None,
                        cover_url=None,
                    ),
                    download_page.tasks_canvas,
                )
                download_page.empty_tasks_label.hide()
                download_page.tasks_layout.insertWidget(0, task_row)
                download_page.view_tabs.setCurrentIndex(1)
                task_row.show()
                favorites_page = window.page("favorites")
                favorites_page._on_snapshot(
                    AccountSnapshot(AccountStatus.SIGNED_IN, "scale-user")
                )
                favorites_page._on_favorites_snapshot(
                    FavoritesSnapshot(
                        "2026-07-16T16:30:00Z",
                        (
                            FavoriteFolderSnapshot(
                                "0",
                                "Default",
                                tuple(
                                    FavoriteItemSnapshot(
                                        str(index),
                                        f"Favorite {index}",
                                        ("Author",),
                                        ("Tag",),
                                    )
                                    for index in range(1, 26)
                                ),
                            ),
                        ),
                    )
                )
                app.processEvents()
                assert len(favorites_page.favorite_cards) == 20
                visible_actions = [
                    task_row.resume_button,
                    task_row.open_images_button,
                    task_row.cancel_button,
                ]
                assert all(button.isVisible() for button in visible_actions)
                for index, button in enumerate(visible_actions):
                    for other in visible_actions[index + 1:]:
                        assert not button.geometry().intersects(other.geometry())
                screenshot_dir = os.environ.get("JM_PHASE7_SCREENSHOT_DIR")
                for page in window.PAGE_ORDER:
                    window.select_page(page)
                    app.processEvents()
                    image = window.grab().toImage()
                    assert not image.isNull()
                    assert image.width() >= 760 and image.height() >= 520
                    colors = {
                        image.pixelColor(x, y).rgba()
                        for x in range(
                            0,
                            image.width(),
                            max(1, image.width() // 20),
                        )
                        for y in range(
                            0,
                            image.height(),
                            max(1, image.height() // 20),
                        )
                    }
                    assert len(colors) >= 5, (page, len(colors))
                    if screenshot_dir:
                        output = Path(screenshot_dir)
                        output.mkdir(parents=True, exist_ok=True)
                        assert image.save(
                            str(
                                output
                                / (
                                    f"scale-{os.environ['QT_SCALE_FACTOR']}"
                                    f"-{page}.png"
                                )
                            )
                        )
                page = window.page("downloads")
                assert not page.general_search_input.geometry().intersects(
                    page.jm_id_search_input.geometry()
                )
                window.select_page("favorites")
                favorites_page.next_page_button.click()
                app.processEvents()
                assert len(favorites_page.favorite_cards) == 5
                assert favorites_page.page_label.text() == "第 2 / 2 页"
                window.select_page("downloads")
                window.select_page("favorites")
                app.processEvents()
                assert len(favorites_page.favorite_cards) == 5
                assert favorites_page.page_label.text() == "第 2 / 2 页"
                window.close()
                app.processEvents()
            """
        )
        for factor in ("1", "1.25", "1.5", "2"):
            with self.subTest(scale_factor=factor):
                environment = os.environ.copy()
                environment["QT_QPA_PLATFORM"] = "offscreen"
                environment["QT_SCALE_FACTOR"] = factor
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=(
                        f"scale={factor}\nstdout:\n{completed.stdout}"
                        f"\nstderr:\n{completed.stderr}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
