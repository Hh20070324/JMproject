from dataclasses import dataclass
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _default_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def _default_resources() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root) if bundle_root else SOURCE_ROOT


@dataclass(frozen=True)
class AppPaths:
    root: Path = SOURCE_ROOT
    resources: Path | None = None

    @property
    def pictures(self) -> Path:
        return self.root / "Pictures"

    @property
    def pdfs(self) -> Path:
        return self.root / "PDFs"

    @property
    def option_file(self) -> Path:
        return self.root / "option.yml"

    @property
    def web(self) -> Path:
        return (self.resources or self.root) / "static"

    def ensure_output_directories(self) -> None:
        self.pictures.mkdir(parents=True, exist_ok=True)
        self.pdfs.mkdir(parents=True, exist_ok=True)


DEFAULT_PATHS = AppPaths(root=_default_root(), resources=_default_resources())
