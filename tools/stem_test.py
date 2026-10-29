"""对比 stem darkening 强度，模拟 DirectWrite 的 enhancedContrast。

同一段文字，四档强度并排，真机抓屏后放大 3 倍看笔画。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401,E402

from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QVBoxLayout, QWidget,
)

OUT = ROOT / "logs" / "font"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"
STEMS = [0.0, 0.35, 0.55, 0.8]


class Board(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("white"))
        self.setPalette(pal)
        from src.ui.hud import InkLabel, hud_font

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)
        self.labels = []
        for s in STEMS:
            InkLabel.STEM = s
            lab = InkLabel(TEXT)
            lab.setFont(hud_font(20))
            lay.addWidget(lab)
            self.labels.append(lab)
        InkLabel.STEM = 0.55
        self.resize(900, 260)


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    b = Board()
    b.move(30, 30)
    b.show()

    def shot():
        for _ in range(8):
            app.processEvents()
        time.sleep(0.35)
        app.processEvents()
        scr = app.primaryScreen()
        dpr = scr.devicePixelRatio()
        g = b.frameGeometry()
        img = scr.grabWindow(0).copy(
            QRect(int(g.x() * dpr), int(g.y() * dpr),
                  int(g.width() * dpr), int(g.height() * dpr))).toImage()
        img.save(str(OUT / "stem_raw.png"))

        print(f"{'stem':>6s} {'墨迹':>6s} {'实心%':>7s} {'中间%':>7s}")
        for i, s in enumerate(STEMS):
            lab = b.labels[i]
            tl = lab.mapToGlobal(lab.rect().topLeft())
            sub = img.copy(QRect(int((tl.x() - g.x()) * dpr),
                                 int((tl.y() - g.y()) * dpr),
                                 int(lab.width() * dpr),
                                 int(lab.height() * dpr)))
            lv = [0] * 8
            tot = 0
            for y in range(sub.height()):
                for x in range(sub.width()):
                    c = sub.pixelColor(x, y)
                    v = (c.red() + c.green() + c.blue()) // 3
                    if v < 252:
                        lv[v // 32] += 1
                        tot += 1
            solid = sum(lv[:2])
            mid = sum(lv[2:6])
            print(f"{s:6.2f} {tot:6d} {100 * solid / max(1, tot):6.1f} "
                  f"{100 * mid / max(1, tot):6.1f}")
            sub.scaled(sub.width() * 3, sub.height() * 3,
                       Qt.IgnoreAspectRatio,
                       Qt.FastTransformation).save(
                str(OUT / f"stem_{s}.png"))
        b.close()
        app.quit()

    QTimer.singleShot(900, shot)
    app.exec()


if __name__ == "__main__":
    main()
