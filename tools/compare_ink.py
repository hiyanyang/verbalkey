"""对比 InkLabel 不同 gamma 的真实观感，输出并排图 + 实心率。

gamma=1.0 等于不做增强（旧版效果）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401,E402

from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

OUT = ROOT / "logs" / "hud"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = ("我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"
        "很多家属第一次找我们，最关心的就是人靠不靠得住。")

GAMMAS = [1.0, 0.75, 0.55, 0.40]


def stats(img: QImage):
    lv = [0] * 8
    tot = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            v = (c.red() + c.green() + c.blue()) // 3
            if v < 252:
                lv[v // 32] += 1
                tot += 1
    return tot, sum(lv[:2])


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    from src.ui import hud as hudmod

    shots = []
    for g in GAMMAS:
        hudmod.InkLabel.GAMMA = g
        h = hudmod.Hud()
        h.show()
        h.show_hud("done", TEXT)
        for _ in range(6):
            app.processEvents()
        time.sleep(0.35)
        app.processEvents()

        scr = app.primaryScreen()
        dpr = scr.devicePixelRatio()
        g0 = h.frameGeometry()
        pm = scr.grabWindow(0).copy(
            QRect(int(g0.x() * dpr), int(g0.y() * dpr),
                  int(g0.width() * dpr), int(g0.height() * dpr)))
        img = pm.toImage()
        # 只截正文区域
        tr = h.text.geometry()
        tl = h.text.mapToGlobal(h.text.rect().topLeft())
        sub = img.copy(QRect(int((tl.x() - g0.x()) * dpr),
                             int((tl.y() - g0.y()) * dpr),
                             int(tr.width() * dpr),
                             int(tr.height() * dpr)))
        tot, solid = stats(sub)
        print(f"gamma={g:4.2f}  墨迹 {tot:6d}  实心 {100 * solid / max(1, tot):5.1f}%")
        shots.append((g, img))
        h.hide()
        h.deleteLater()
        app.processEvents()

    W = max(i.width() for _, i in shots)
    H = sum(i.height() + 10 for _, i in shots)
    out = QImage(W, H, QImage.Format_RGB32)
    out.fill(QColor("#DDDDDD"))
    p = QPainter(out)
    y = 0
    for _, i in shots:
        p.drawImage(0, y, i)
        y += i.height() + 10
    p.end()
    out.save(str(OUT / "gamma_compare.png"))
    print("saved", out.width(), out.height())
    hudmod.InkLabel.GAMMA = 0.55     # 还原默认


if __name__ == "__main__":
    main()
