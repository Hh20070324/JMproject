"""Generate the application ICO from the bundled padding book SVG."""

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    PROJECT_ROOT
    / "jm_downloader"
    / "qt"
    / "resources"
    / "icons"
    / "book.svg"
)
OUTPUT = (
    PROJECT_ROOT
    / "jm_downloader"
    / "qt"
    / "resources"
    / "app.ico"
)
ICON_COLOR = "#2e7d57"
ICON_SIZES = (16, 24, 32, 48, 256)


def generate(output: Path = OUTPUT) -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if "currentColor" not in source:
        raise RuntimeError("book.svg no longer exposes currentColor")
    payload = source.replace("currentColor", ICON_COLOR).encode("utf-8")
    renderer = QSvgRenderer(QByteArray(payload))
    if not renderer.isValid():
        raise RuntimeError("book.svg cannot be rendered")

    canvas = QImage(256, 256, QImage.Format.Format_RGBA8888)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    renderer.render(painter)
    painter.end()
    if canvas.isNull():
        raise RuntimeError("application icon rendering failed")

    rgba = bytes(canvas.bits()[: 256 * 256 * 4])
    image = Image.frombytes("RGBA", (256, 256), rgba)
    image.save(
        output,
        format="ICO",
        sizes=tuple((size, size) for size in ICON_SIZES),
    )


if __name__ == "__main__":
    generate()
