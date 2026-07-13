import configparser
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile
import yaml

from ..settings import (
    AppPaths,
    AppSettings,
    DEFAULT_PATHS,
    SettingsValidationError,
    UnsupportedSettingsVersion,
)


class SettingsStoreError(Exception):
    pass


class SettingsRecoveryError(SettingsStoreError):
    pass


class SettingsStore:
    def __init__(self, paths: AppPaths = DEFAULT_PATHS):
        self.paths = paths
        self.settings: AppSettings | None = None
        self.last_recovery_backup: Path | None = None

    def load(self) -> AppSettings:
        self.last_recovery_backup = None
        settings_path = self.paths.settings_file
        if not settings_path.is_file():
            settings = self._migrate_initial_settings()
            self.save(settings)
            return settings

        try:
            raw = settings_path.read_bytes()
        except OSError as error:
            raise SettingsStoreError(f"无法读取设置：{error}") from error

        try:
            data = json.loads(raw.decode("utf-8"))
            settings = AppSettings.from_dict(data)
        except UnsupportedSettingsVersion:
            raise
        except (UnicodeError, json.JSONDecodeError, SettingsValidationError) as error:
            return self._recover_corrupt_settings(raw, error)
        self.settings = settings
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        payload = (
            json.dumps(
                settings.to_dict(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        self._write_atomic(self.paths.settings_file, payload)
        self.settings = settings

    def reset(self) -> AppSettings:
        settings = AppSettings()
        self.save(settings)
        return settings

    def _recover_corrupt_settings(
        self, raw: bytes, original_error: Exception
    ) -> AppSettings:
        backup_path = self._next_backup_path()
        try:
            self._write_atomic(backup_path, raw)
        except SettingsStoreError as error:
            raise SettingsRecoveryError(
                f"设置文件已损坏，且无法创建备份：{error}"
            ) from original_error
        self.last_recovery_backup = backup_path

        settings = AppSettings()
        try:
            self.save(settings)
        except SettingsStoreError as error:
            raise SettingsRecoveryError(
                f"设置文件已备份到 {backup_path}，但无法恢复默认设置：{error}"
            ) from original_error
        return settings

    def _migrate_initial_settings(self) -> AppSettings:
        settings = AppSettings()
        theme = self._read_legacy_theme()
        image_concurrency = self._read_legacy_image_concurrency()
        if theme is not None:
            settings = replace(settings, theme=theme)
        if image_concurrency is not None:
            settings = replace(
                settings,
                image_concurrency=image_concurrency,
            )
        settings.validate()
        return settings

    def _read_legacy_theme(self) -> str | None:
        path = self.paths.legacy_settings_file
        if not path.is_file():
            return None
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
            value = parser.get("appearance", "theme", fallback="").strip().lower()
        except (OSError, UnicodeError, configparser.Error):
            return None
        return value if value in {"light", "dark"} else None

    def _read_legacy_image_concurrency(self) -> int | None:
        path = self.paths.option_file
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                return None
            download = data.get("download")
            if not isinstance(download, Mapping):
                return None
            threading = download.get("threading")
            if not isinstance(threading, Mapping):
                return None
            value = threading.get("image", threading.get("batch_count"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if type(value) is int and 1 <= value <= 64:
            return value
        return None

    def _next_backup_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.paths.settings_file.with_name(
            f"{self.paths.settings_file.name}.corrupt-{stamp}"
        )
        candidate = base
        suffix = 1
        while candidate.exists():
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        return candidate

    @staticmethod
    def _write_atomic(target: Path, payload: bytes) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SettingsStoreError(f"无法创建设置目录：{error}") from error

        save_file = QSaveFile(str(target))
        save_file.setDirectWriteFallback(False)
        if not save_file.open(QIODevice.OpenModeFlag.WriteOnly):
            raise SettingsStoreError(save_file.errorString())

        written = save_file.write(payload)
        if written != len(payload):
            error = save_file.errorString()
            save_file.cancelWriting()
            raise SettingsStoreError(error or "设置文件写入不完整")
        if not save_file.commit():
            raise SettingsStoreError(save_file.errorString())
