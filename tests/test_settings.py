from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from jm_downloader.settings import (
    AppPaths,
    AppSettings,
    SettingsValidationError,
    UnsupportedSettingsVersion,
    serialize_portable_path,
)


class AppPathsTests(unittest.TestCase):
    def test_keeps_user_data_beside_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            paths = AppPaths(root=root)

            self.assertEqual(paths.pictures, root / "Pictures")
            self.assertEqual(paths.pdfs, root / "PDFs")
            self.assertEqual(paths.option_file, root / "option.yml")
            self.assertEqual(paths.settings_file, root / "settings.json")
            self.assertEqual(paths.legacy_settings_file, root / "settings.ini")
            self.assertEqual(paths.logs, root / "logs")

    def test_resolves_relative_directories_against_current_program_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_root = Path(temp_dir) / "first"
            second_root = Path(temp_dir) / "moved"
            settings = AppSettings(
                pictures_directory="data/Pictures",
                pdf_directory="data/PDFs",
            )

            first = AppPaths(first_root).with_settings(settings)
            moved = AppPaths(second_root).with_settings(settings)

            self.assertEqual(first.pictures, (first_root / "data/Pictures").resolve())
            self.assertEqual(moved.pictures, (second_root / "data/Pictures").resolve())
            self.assertEqual(moved.pdfs, (second_root / "data/PDFs").resolve())

    def test_serializes_internal_path_as_portable_and_external_as_absolute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            internal = root / "data" / "images"
            external = Path(temp_dir) / "shared"

            self.assertEqual(
                serialize_portable_path(root, internal),
                "data/images",
            )
            self.assertEqual(
                serialize_portable_path(root, external),
                str(external.resolve()),
            )


class AppSettingsTests(unittest.TestCase):
    def test_round_trips_schema_v1_and_ignores_unknown_fields(self):
        settings = AppSettings(
            pictures_directory="media/images",
            pdf_directory="media/pdfs",
            max_concurrent_tasks=4,
            image_concurrency=24,
            log_level="DEBUG",
            window_width=1280,
            window_height=800,
            startup_page="library",
            theme="dark",
        )
        data = settings.to_dict()
        data["future_field"] = {"ignored": True}

        self.assertEqual(AppSettings.from_dict(data), settings)

    def test_missing_groups_use_defaults(self):
        settings = AppSettings.from_dict({"schema_version": 1})
        self.assertEqual(settings, AppSettings())

    def test_rejects_future_schema_without_coercion(self):
        with self.assertRaises(UnsupportedSettingsVersion):
            AppSettings.from_dict({"schema_version": 2})

    def test_rejects_invalid_values(self):
        invalid_settings = (
            replace(AppSettings(), pictures_directory=" "),
            replace(AppSettings(), max_concurrent_tasks=0),
            replace(AppSettings(), image_concurrency=True),
            replace(AppSettings(), log_level="TRACE"),
            replace(AppSettings(), log_level=[]),
            replace(AppSettings(), window_width=759),
            replace(AppSettings(), window_height=519),
            replace(AppSettings(), startup_page="missing"),
            replace(AppSettings(), startup_page={}),
            replace(AppSettings(), theme="system"),
            replace(AppSettings(), theme=[]),
            replace(AppSettings(), pictures_directory="../outside"),
            replace(
                AppSettings(), pictures_directory=r"nested\..\..\outside"
            ),
            replace(AppSettings(), pictures_directory=r"C:relative"),
            replace(AppSettings(), pictures_directory=r"\root-relative"),
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(SettingsValidationError):
                    settings.validate()

    def test_allows_absolute_drive_and_unc_directories(self):
        for directory in (
            r"C:\Pictures",
            r"\\server\share\Pictures",
            r"nested\..\Pictures",
        ):
            with self.subTest(directory=directory):
                replace(
                    AppSettings(), pictures_directory=directory
                ).validate()


if __name__ == "__main__":
    unittest.main()
