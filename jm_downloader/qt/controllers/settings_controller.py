from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ...settings import AppSettings
from ..settings_store import SettingsStore


class SettingsController(QObject):
    settings_changed = Signal(object)
    save_succeeded = Signal(object)
    save_failed = Signal(str)

    def __init__(
        self,
        store: SettingsStore,
        parent=None,
        settings_validator: Callable[[AppSettings], None] | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self._settings_validator = settings_validator
        self._settings = self._load_initial_settings()

    @property
    def settings(self) -> AppSettings:
        return self._settings

    @property
    def root_path(self) -> Path:
        paths = getattr(self.store, "paths", None)
        root = getattr(paths, "root", None)
        if root is not None:
            return Path(root).resolve()
        return Path.cwd().resolve()

    @Slot(object)
    def save(self, settings: AppSettings) -> bool:
        try:
            if self._settings_validator is not None:
                self._settings_validator(settings)
            saved = self.store.save(settings)
            self._settings = self._resolved_result(saved, settings)
        except Exception as error:
            self.save_failed.emit(str(error) or "设置保存失败")
            return False

        self.settings_changed.emit(self._settings)
        self.save_succeeded.emit(self._settings)
        return True

    @Slot()
    def reset_defaults(self) -> bool:
        try:
            defaults = AppSettings()
            if self._settings_validator is not None:
                self._settings_validator(defaults)
            reset = getattr(self.store, "reset", None)
            if reset is None:
                saved = self.store.save(defaults)
                self._settings = self._resolved_result(saved, defaults)
            else:
                restored = reset()
                self._settings = self._resolved_result(restored, AppSettings())
        except Exception as error:
            self.save_failed.emit(str(error) or "默认设置恢复失败")
            return False

        self.settings_changed.emit(self._settings)
        self.save_succeeded.emit(self._settings)
        return True

    def _load_initial_settings(self) -> AppSettings:
        current = getattr(self.store, "settings", None)
        if isinstance(current, AppSettings):
            return current
        return self.store.load()

    def _resolved_result(
        self,
        result,
        fallback: AppSettings,
    ) -> AppSettings:
        if isinstance(result, AppSettings):
            return result
        current = getattr(self.store, "settings", None)
        if isinstance(current, AppSettings):
            return current
        return fallback
