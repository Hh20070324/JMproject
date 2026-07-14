from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import resource_path


@lru_cache(maxsize=128)
def svg_icon(name: str, color: str = "#748079", size: int = 64) -> QIcon:
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz-" for character in name):
        raise ValueError("invalid icon name")
    if type(size) is not int or size < 1 or size > 256:
        raise ValueError("invalid icon size")

    try:
        source = resource_path(f"icons/{name}.svg").read_text(encoding="utf-8")
    except OSError:
        return QIcon()

    payload = source.replace("currentColor", color).encode("utf-8")
    renderer = QSvgRenderer(QByteArray(payload))
    if not renderer.isValid():
        return QIcon()

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def search_icon(color: str = "#748079") -> QIcon:
    return svg_icon("search", color)


def arrow_icon(direction: str, color: str = "#748079") -> QIcon:
    if direction not in ("left", "right"):
        raise ValueError("direction must be left or right")
    return svg_icon(f"arrow-{direction}", color)


__all__ = ["arrow_icon", "search_icon", "svg_icon"]
