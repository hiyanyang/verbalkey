"""找出在 Qt6 + Windows 上真正能关掉 ClearType 彩边的方法。

每种方案开一个不透明窗口，抓真实屏幕统计彩边比例。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QFont, QImage, QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

TEXT = "我这边是做陪护 hello 123"


def make_font(px: int, strategy=None, hinting=None) -> QFont:
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(px)
    if strategy is not None:
        f.setStyleStrategy(strategy)
    if hinting is not None:
        f.setHintingPreference(hinting)
    return f


class Painted(QWidget):
    """自己画文字：先画进 ARGB32 离屏图（强制灰度 AA），再贴到不透明窗口上。"""

    def __init__(self, px: int):
        super().__init__()
        self.px = px
        self.font_obj = make_font(px)
        self.setFixedSize(300, 44)

    def paintEvent(self, _e):
        r = self.devicePixelRatioF()
        w = int(self.width() * r)
        h = int(self.height() * r)
        off = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        off.setDevicePixelRatio(r)
        off.fill(Qt.transparent)
        p = QPainter(off)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.font_obj)
        p.setPen(QColor("#1D1D1F"))
        p.drawText(QRect(0, 0, self.width(), self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, TEXT)
        p.end()
        q = QPainter(self)
        q.drawImage(0, 0, off)
        q.end()


def measure(win: QWidget, target: QWidget) -> tuple[float, int, int]:
    scr = QApplication.primaryScreen()
    r = scr.devicePixelRatio()
    tl = target.mapToGlobal(target.rect().topLeft())
    pm = scr.grabWindow(0).copy(
        QRect(int(tl.x() * r), int(tl.y() * r),
              int(target.width() * r), int(target.height() * r))).toImage()
    col = tot = 0
    for y in range(pm.height()):
        for x in range(pm.width()):
            c = pm.pixelColor(x, y)
            rr, gg, bb = c.red(), c.green(), c.blue()
            if (rr, gg, bb) != (255, 255, 255):
                tot += 1
                if abs(rr - gg) > 8 or abs(gg - bb) > 8 or abs(rr - bb) > 8:
                    col += 1
    return 100.0 * col / max(1, tot), col, tot


def run_case(name, widget, window_fn):
    win = window_fn(widget)
    scr = QApplication.primaryScreen()
    g = scr.availableGeometry()
    win.move(g.x() + 60, g.y() + 60)
    win.show()
    app = QApplication.instance()
    for _ in range(6):
        app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    r = scr.devicePixelRatio()
    tl = widget.mapToGlobal(widget.rect().topLeft())
    shot = scr.grabWindow(0).copy(
        QRect(int(tl.x() * r), int(tl.y() * r),
              int(widget.width() * r), int(widget.height() * r)))
    out = ROOT / "logs" / "font"
    out.mkdir(parents=True, exist_ok=True)
    shot.save(str(out / f"probe_{name.split()[0]}.png"))
    pct, col, tot = measure(win, widget)
    print(f"{name:36s} 彩边 {pct:5.1f}%  ({col}/{tot})  尺寸 {shot.width()}x{shot.height()}")
    win.close()
    app.processEvents()


def wrap(widget):
    win = QWidget()
    win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    win.setAutoFillBackground(True)
    pal = win.palette()
    pal.setColor(QPalette.Window, QColor("white"))
    win.setPalette(pal)
    lay = QVBoxLayout(win)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.addWidget(widget)
    win.adjustSize()
    return win


def main():
    global app
    app = QApplication([])
    app.setStyle("Fusion")

    def label(strategy=None, hinting=None, px=16):
        lb = QLabel(TEXT)
        lb.setFont(make_font(px, strategy, hinting))
        pal = lb.palette()
        pal.setColor(QPalette.WindowText, QColor("#1D1D1F"))
        lb.setPalette(pal)
        return lb

    run_case("1 默认（ClearType）", label(), wrap)
    run_case("2 NoSubpixelAntialias", label(QFont.NoSubpixelAntialias), wrap)
    run_case("3 PreferAntialias|NoSubpixel", label(QFont.PreferAntialias | QFont.NoSubpixelAntialias), wrap)
    run_case("4 NoSubpixel + 无hint", label(QFont.NoSubpixelAntialias, QFont.PreferNoHinting), wrap)
    run_case("5 自绘到 ARGB 离屏图", Painted(16), wrap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
