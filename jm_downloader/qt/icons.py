from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


@lru_cache(maxsize=8)
def search_icon(color: str = "#748079") -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawEllipse(QRectF(9, 9, 34, 34))
    painter.drawLine(39, 39, 55, 55)
    painter.end()
    return QIcon(pixmap)


@lru_cache(maxsize=16)
def arrow_icon(direction: str, color: str = "#748079") -> QIcon:
    if direction not in ("left", "right"):
        raise ValueError("direction must be left or right")

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if direction == "left":
        painter.drawLine(40, 12, 22, 32)
        painter.drawLine(22, 32, 40, 52)
    else:
        painter.drawLine(24, 12, 42, 32)
        painter.drawLine(42, 32, 24, 52)
    painter.end()
    return QIcon(pixmap)


__all__ = ["arrow_icon", "search_icon"]
