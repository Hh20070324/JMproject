from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jm_downloader.qt.settings_store import SettingsStore, SettingsStoreError
from jm_downloader.settings import (
    AppPaths,
    AppSettings,
    UnsupportedSettingsVersion,
)


class QtSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(self.root)
        self.store = SettingsStore(self.paths)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_load_migrates_ini_theme_and_legacy_batch_count(self):
        self.paths.legacy_settings_file.write_text(
            "[appearance]\ntheme=dark\n",
            encoding="utf-8",
        )
        self.paths.option_file.write_text(
            "download:\n  threading:\n    batch_count: 23\n",
            encoding="utf-8",
        )

        settings = self.store.load()

        self.assertEqual(settings.theme, "dark")
        self.assertEqual(settings.image_concurrency, 23)
        self.assertTrue(self.paths.settings_file.is_file())
        self.assertEqual(SettingsStore(self.paths).load(), settings)

    def test_new_image_key_takes_precedence_during_migration(self):
        self.paths.option_file.write_text(
            "download:\n  threading:\n    image: 12\n    batch_count: 31\n",
            encoding="utf-8",
        )
        self.assertEqual(self.store.load().image_concurrency, 12)

    def test_malformed_legacy_threading_config_is_ignored(self):
        for value in ("[]", "null", "broken"):
            with self.subTest(value=value):
                self.paths.settings_file.unlink(missing_ok=True)
                self.paths.option_file.write_text(
                    f"download:\n  threading: {value}\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    self.store.load().image_concurrency,
                    AppSettings().image_concurrency,
                )

    def test_save_and_reset_round_trip(self):
        expected = replace(
            AppSettings(),
            pictures_directory="portable/images",
            pdf_directory="portable/pdfs",
            max_concurrent_tasks=5,
            theme="dark",
        )
        self.store.save(expected)

        self.assertEqual(self.store.load(), expected)
        self.assertEqual(self.store.reset(), AppSettings())
        self.assertEqual(self.store.load(), AppSettings())

    def test_corrupt_json_is_backed_up_and_replaced_with_defaults(self):
        raw = b'{"schema_version": 1, broken'
        self.paths.settings_file.write_bytes(raw)

        settings = self.store.load()

        self.assertEqual(settings, AppSettings())
        backup = self.store.last_recovery_backup
        self.assertIsNotNone(backup)
        self.assertEqual(backup.read_bytes(), raw)
        restored = json.loads(self.paths.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(AppSettings.from_dict(restored), AppSettings())

    def test_invalid_values_are_treated_as_corruption(self):
        self.paths.settings_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "download": {"max_concurrent_tasks": 0},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.store.load(), AppSettings())
        self.assertIsNotNone(self.store.last_recovery_backup)

    def test_unhashable_enum_value_is_treated_as_corruption(self):
        self.paths.settings_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "logging": {"level": []},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.store.load(), AppSettings())
        self.assertIsNotNone(self.store.last_recovery_backup)

    def test_recovery_reports_backup_when_default_write_fails(self):
        raw = b"broken"
        self.paths.settings_file.write_bytes(raw)
        original_write = self.store._write_atomic

        def fail_default_write(target, payload):
            if target == self.paths.settings_file:
                raise SettingsStoreError("read only")
            return original_write(target, payload)

        with patch.object(
            self.store,
            "_write_atomic",
            side_effect=fail_default_write,
        ):
            with self.assertRaisesRegex(
                SettingsStoreError,
                "已备份到.*无法恢复默认设置",
            ):
                self.store.load()

        self.assertEqual(self.paths.settings_file.read_bytes(), raw)
        self.assertIsNotNone(self.store.last_recovery_backup)
        self.assertEqual(self.store.last_recovery_backup.read_bytes(), raw)

    def test_future_schema_is_refused_without_rewriting_or_backup(self):
        raw = b'{"schema_version": 2}'
        self.paths.settings_file.write_bytes(raw)

        with self.assertRaises(UnsupportedSettingsVersion):
            self.store.load()

        self.assertEqual(self.paths.settings_file.read_bytes(), raw)
        self.assertEqual(list(self.root.glob("settings.json.corrupt-*")), [])

    def test_atomic_writer_disables_direct_write_fallback(self):
        class FakeSaveFile:
            instance = None

            def __init__(self, filename):
                self.filename = filename
                self.direct_fallback = None
                self.payload = None
                FakeSaveFile.instance = self

            def setDirectWriteFallback(self, enabled):
                self.direct_fallback = enabled

            def open(self, _mode):
                return True

            def write(self, payload):
                self.payload = payload
                return len(payload)

            def commit(self):
                return True

            def errorString(self):
                return ""

        with patch(
            "jm_downloader.qt.settings_store.QSaveFile",
            FakeSaveFile,
        ):
            SettingsStore._write_atomic(self.paths.settings_file, b"settings")

        self.assertFalse(FakeSaveFile.instance.direct_fallback)
        self.assertEqual(FakeSaveFile.instance.payload, b"settings")

    def test_failed_atomic_commit_reports_error_and_preserves_old_file(self):
        self.paths.settings_file.write_bytes(b"old")

        class FailedSaveFile:
            def __init__(self, _filename):
                pass

            def setDirectWriteFallback(self, _enabled):
                pass

            def open(self, _mode):
                return True

            def write(self, payload):
                return len(payload)

            def commit(self):
                return False

            def errorString(self):
                return "commit failed"

        with patch(
            "jm_downloader.qt.settings_store.QSaveFile",
            FailedSaveFile,
        ):
            with self.assertRaisesRegex(SettingsStoreError, "commit failed"):
                self.store.save(AppSettings())

        self.assertEqual(self.paths.settings_file.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
