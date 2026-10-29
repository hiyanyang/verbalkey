"""验证：文字在垂直方向错开半个物理像素，清晰度会不会变。

150% 缩放下，逻辑 y=20 → 物理 30（对齐），y=21 → 物理 31.5（半像素）。
如果「深浅不一致」是这个原因，两行文字的实心率应该差很多。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401,E402

from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

OUT = ROOT / "logs" / "font"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "我这边是做陪护的，主要派护理员"
OFFSETS = [20, 21, 22, 23]     # 偶数/奇数交替


class Board(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("white"))
        self.setPalette(pal)
        self.labels = []
        for i, y in enumerate(OFFSETS):
            lab = QLabel(TEXT, self)
            f = QFont("Microsoft YaHei UI")
            f.setPixelSize(18)
            f.setStyleStrategy(QFont.NoSubpixelAntialias)
            lab.setFont(f)
            lab.setStyleSheet("color:#1D1D1F;")
            lab.move(16, y)
            lab.resize(320, 30)
            lab.show()
            self.labels.append(lab)
        self.resize(360, 120)


def stats(img):
    levels = [0] * 8
    total = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            v = (c.red() + c.green() + c.blue()) // 3
            if v < 252:
                levels[v // 32] += 1
                total += 1
    solid = sum(levels[:2])
    return total, solid


def main():
    app = QApplication.instance() or QApplication([])
    b = Board()
    b.move(40, 40)
    b.show()

    def shot():
        for _ in range(8):
            app.processEvents()
        time.sleep(0.3)
        app.processEvents()
        scr = app.primaryScreen()
        dpr = scr.devicePixelRatio()
        g = b.frameGeometry()
        img = scr.grabWindow(0).copy(
            QRect(int(g.x() * dpr), int(g.y() * dpr),
                  int(g.width() * dpr), int(g.height() * dpr))).toImage()
        img.save(str(OUT / "baseline_raw.png"))

        print(f"{'逻辑y':>6s} {'物理y':>7s} {'墨迹':>6s} {'实心%':>7s}")
        for i, y in enumerate(OFFSETS):
            lab = b.labels[i]
            tl = lab.mapToGlobal(lab.rect().topLeft())
            rx = int((tl.x() - g.x()) * dpr)
            ry = int((tl.y() - g.y()) * dpr)
            sub = img.copy(QRect(rx, ry, int(lab.width() * dpr),
                                 int(lab.height() * dpr)))
            tot, solid = stats(sub)
            print(f"{y:6d} {y * dpr:7.1f} {tot:6d} {100 * solid / max(1, tot):6.1f}")
            sub.scaled(sub.width() * 3, sub.height() * 3,
                       Qt.IgnoreAspectRatio,
                       Qt.FastTransformation).save(
                str(OUT / f"baseline_y{y}.png"))
        b.close()
        app.quit()

    QTimer.singleShot(800, shot)
    app.exec()


if __name__ == "__main__":
    main()
