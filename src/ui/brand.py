"""产品外观：主色与图标。托盘、窗口、exe 都用同一份画法。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap,
)

PINK = "#FF2D55"


def draw_icon(size: int = 256, color: str = PINK) -> QPixmap:
    """粉色圆角矩形 + 白色字母 V（Arial Black 字形）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    u = size / 64.0
    body = QPainterPath()
    body.addRoundedRect(4 * u, 4 * u, 56 * u, 56 * u, 14 * u, 14 * u)
    p.fillPath(body, QColor(color))

    # V 直接用 Arial Black 的字形轮廓填充：笔画厚、下角是尖的，
    # 比手画的直线更饱满。用 addText 拿路径再整体居中，
    # 这样不同字号下位置都不会偏。
    f = QFont("Arial Black")
    f.setPixelSize(max(6, int(round(42 * u))))
    path = QPainterPath()
    path.addText(0.0, 0.0, f, "V")
    br = path.boundingRect()
    path.translate(32 * u - br.center().x(), 32 * u - br.center().y())
    p.fillPath(path, QColor("white"))
    p.end()
    return pm


def make_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(draw_icon(s))
    return icon
