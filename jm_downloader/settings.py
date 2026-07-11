from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    root: Path = PROJECT_ROOT

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
        return self.root / "static"

    def ensure_output_directories(self) -> None:
        self.pictures.mkdir(parents=True, exist_ok=True)
        self.pdfs.mkdir(parents=True, exist_ok=True)


DEFAULT_PATHS = AppPaths()
