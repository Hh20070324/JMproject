from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import sys


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_SCHEMA_VERSION = 1


class SettingsError(Exception):
    pass


class SettingsValidationError(SettingsError):
    pass


class UnsupportedSettingsVersion(SettingsError):
    pass


def _default_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def resolve_portable_path(root: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_portable_path(root: Path, selected_path: str | Path) -> str:
    resolved_root = root.resolve()
    resolved_path = Path(selected_path).resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError:
        return str(resolved_path)
    value = relative.as_posix()
    return value or "."


def validate_portable_directory(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SettingsValidationError(f"{label}无效")
    windows_path = PureWindowsPath(value)
    if windows_path.root and not windows_path.drive:
        raise SettingsValidationError(f"{label}不能使用无盘符根路径")
    if windows_path.drive and not windows_path.root:
        raise SettingsValidationError(f"{label}不能使用盘符相对路径")
    if not windows_path.is_absolute() and _relative_path_escapes(windows_path):
        raise SettingsValidationError(f"{label}的相对路径不能超出程序目录")


def _relative_path_escapes(path: PureWindowsPath) -> bool:
    depth = 0
    for part in path.parts:
        if part == "..":
            if depth == 0:
                return True
            depth -= 1
        else:
            depth += 1
    return False


@dataclass(frozen=True)
class AppSettings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    pictures_directory: str = "Pictures"
    pdf_directory: str = "PDFs"
    max_concurrent_tasks: int = 2
    image_concurrency: int = 16
    log_level: str = "INFO"
    window_width: int = 1100
    window_height: int = 720
    startup_page: str = "downloads"
    theme: str = "light"

    def validate(self) -> None:
        if type(self.schema_version) is not int:
            raise SettingsValidationError("配置版本必须是整数")
        if self.schema_version > SETTINGS_SCHEMA_VERSION:
            raise UnsupportedSettingsVersion(
                f"配置版本 {self.schema_version} 高于程序支持的版本 "
                f"{SETTINGS_SCHEMA_VERSION}"
            )
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise SettingsValidationError("不支持的配置版本")

        for label, value in (
            ("图片目录", self.pictures_directory),
            ("PDF 目录", self.pdf_directory),
        ):
            validate_portable_directory(label, value)

        self._validate_integer(
            "最大同时任务数", self.max_concurrent_tasks, minimum=1, maximum=8
        )
        self._validate_integer(
            "图片并发数", self.image_concurrency, minimum=1, maximum=64
        )
        self._validate_integer(
            "窗口宽度", self.window_width, minimum=760, maximum=10000
        )
        self._validate_integer(
            "窗口高度", self.window_height, minimum=520, maximum=10000
        )

        if not isinstance(self.log_level, str) or self.log_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
        }:
            raise SettingsValidationError("日志级别无效")
        if not isinstance(self.startup_page, str) or self.startup_page not in {
            "downloads",
            "favorites",
            "library",
            "settings",
        }:
            raise SettingsValidationError("启动页面无效")
        if not isinstance(self.theme, str) or self.theme not in {
            "light",
            "dark",
        }:
            raise SettingsValidationError("主题模式无效")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "paths": {
                "pictures": self.pictures_directory,
                "pdfs": self.pdf_directory,
            },
            "download": {
                "max_concurrent_tasks": self.max_concurrent_tasks,
                "image_concurrency": self.image_concurrency,
            },
            "logging": {"level": self.log_level},
            "window": {
                "width": self.window_width,
                "height": self.window_height,
                "startup_page": self.startup_page,
            },
            "appearance": {"theme": self.theme},
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "AppSettings":
        if not isinstance(data, Mapping):
            raise SettingsValidationError("配置根节点必须是对象")

        defaults = cls()
        schema_version = data.get("schema_version", SETTINGS_SCHEMA_VERSION)
        if type(schema_version) is not int:
            raise SettingsValidationError("配置版本必须是整数")
        if schema_version > SETTINGS_SCHEMA_VERSION:
            raise UnsupportedSettingsVersion(
                f"配置版本 {schema_version} 高于程序支持的版本 "
                f"{SETTINGS_SCHEMA_VERSION}"
            )

        paths = cls._group(data, "paths")
        download = cls._group(data, "download")
        logging = cls._group(data, "logging")
        window = cls._group(data, "window")
        appearance = cls._group(data, "appearance")

        settings = cls(
            schema_version=schema_version,
            pictures_directory=paths.get(
                "pictures", defaults.pictures_directory
            ),
            pdf_directory=paths.get("pdfs", defaults.pdf_directory),
            max_concurrent_tasks=download.get(
                "max_concurrent_tasks", defaults.max_concurrent_tasks
            ),
            image_concurrency=download.get(
                "image_concurrency", defaults.image_concurrency
            ),
            log_level=logging.get("level", defaults.log_level),
            window_width=window.get("width", defaults.window_width),
            window_height=window.get("height", defaults.window_height),
            startup_page=window.get("startup_page", defaults.startup_page),
            theme=appearance.get("theme", defaults.theme),
        )
        settings.validate()
        return settings

    @staticmethod
    def _group(data: Mapping, name: str) -> Mapping:
        value = data.get(name, {})
        if not isinstance(value, Mapping):
            raise SettingsValidationError(f"配置项 {name} 必须是对象")
        return value

    @staticmethod
    def _validate_integer(
        label: str, value: int, minimum: int, maximum: int
    ) -> None:
        if type(value) is not int or not minimum <= value <= maximum:
            raise SettingsValidationError(
                f"{label}必须在 {minimum} 到 {maximum} 之间"
            )

@dataclass(frozen=True)
class AppPaths:
    root: Path = SOURCE_ROOT
    pictures_override: Path | None = None
    pdfs_override: Path | None = None

    @property
    def pictures(self) -> Path:
        return self.pictures_override or self.root / "Pictures"

    @property
    def pdfs(self) -> Path:
        return self.pdfs_override or self.root / "PDFs"

    @property
    def option_file(self) -> Path:
        return self.root / "option.yml"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def tasks_file(self) -> Path:
        return self.root / "tasks.json"

    @property
    def account_file(self) -> Path:
        return self.root / "account.dat"

    @property
    def favorites_file(self) -> Path:
        return self.root / "favorites.dat"

    @property
    def legacy_settings_file(self) -> Path:
        return self.root / "settings.ini"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def with_settings(self, settings: AppSettings) -> "AppPaths":
        settings.validate()
        return AppPaths(
            root=self.root,
            pictures_override=resolve_portable_path(
                self.root, settings.pictures_directory
            ),
            pdfs_override=resolve_portable_path(
                self.root, settings.pdf_directory
            ),
        )

    def ensure_output_directories(self) -> None:
        self.pictures.mkdir(parents=True, exist_ok=True)
        self.pdfs.mkdir(parents=True, exist_ok=True)


DEFAULT_PATHS = AppPaths(root=_default_root())
