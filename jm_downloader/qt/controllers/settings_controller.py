from collections.abc import Callable
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal, Slot

from ...settings import AppSettings
from ...option_config import probe_api_route
from ..settings_store import SettingsStore


class SettingsController(QObject):
    settings_changed = Signal(object)
    save_succeeded = Signal(object)
    save_failed = Signal(str)
    route_test_succeeded = Signal(str, int)
    route_test_failed = Signal(str, str)

    def __init__(
        self,
        store: SettingsStore,
        parent=None,
        settings_validator: Callable[[AppSettings], None] | None = None,
        route_probe: Callable[[Path, str], int] | None = probe_api_route,
    ):
        super().__init__(parent)
        self.store = store
        self._settings_validator = settings_validator
        self._route_probe = route_probe
        self._route_test_lock = threading.Lock()
        self._route_test_generation = 0
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

    @Slot(str)
    def test_api_route(self, route: str) -> None:
        if self._route_probe is None:
            self.route_test_failed.emit(route, "当前环境未提供路线测试")
            return
        with self._route_test_lock:
            self._route_test_generation += 1
            generation = self._route_test_generation
        thread = threading.Thread(
            target=self._run_route_test,
            args=(generation, route),
            name="api-route-probe",
            daemon=True,
        )
        thread.start()

    def _run_route_test(self, generation: int, route: str) -> None:
        try:
            elapsed_ms = self._route_probe(
                self.store.paths.option_file,
                route,
            )
        except Exception:
            with self._route_test_lock:
                if generation != self._route_test_generation:
                    return
            self.route_test_failed.emit(
                route,
                "路线不可用，请切换其他路线或使用自动选择",
            )
            return
        with self._route_test_lock:
            if generation != self._route_test_generation:
                return
        self.route_test_succeeded.emit(route, int(elapsed_ms))

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
