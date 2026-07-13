from enum import Enum
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from ..settings import DEFAULT_PATHS


class Theme(str, Enum):
    LIGHT = "light"
    DARK = "dark"


def _coerce_theme(theme: Theme | str) -> Theme:
    try:
        return theme if isinstance(theme, Theme) else Theme(theme)
    except (TypeError, ValueError):
        return Theme.LIGHT


def resource_path(filename: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        return Path(bundle_root) / "jm_downloader" / "qt" / "resources" / filename
    return Path(__file__).resolve().parent / "resources" / filename


def load_stylesheet(theme: Theme = Theme.LIGHT) -> str:
    selected_theme = _coerce_theme(theme)
    try:
        return resource_path(f"styles_{selected_theme.value}.qss").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""


class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self._settings = settings
        if self._settings is None:
            self._settings = QSettings(
                str(DEFAULT_PATHS.root / "settings.ini"),
                QSettings.Format.IniFormat,
            )
        saved_theme = self._settings.value("appearance/theme", Theme.LIGHT.value)
        self._theme = _coerce_theme(saved_theme)

    @property
    def theme(self) -> Theme:
        return self._theme

    def apply(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(load_stylesheet(self._theme))

    def set_theme(self, theme: Theme | str) -> None:
        selected_theme = _coerce_theme(theme)
        changed = selected_theme != self._theme
        self._theme = selected_theme
        self.apply()
        self._settings.setValue("appearance/theme", selected_theme.value)
        self._settings.sync()
        if changed:
            self.theme_changed.emit(selected_theme.value)
