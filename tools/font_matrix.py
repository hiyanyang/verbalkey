"""浮窗字体对比：在真机屏幕上并排渲染 3 字体 × 3 对比度增强。

关键指标：
  实心%  灰度 < 64 的像素占比 —— 越高笔画越"实"
  中间%  64..191 的灰度占比 —— 就是人眼看到的"深浅不一致"
  浅边%  192..255 的占比
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401,E402

from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

OUT = ROOT / "logs" / "font"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"
INK_RGB = 29          # #1D1D1F
SPAN = 255 - INK_RGB  # 墨到背景的跨度


def gamma_lut(g: float) -> bytes:
    """把覆盖率 c 变成 c**g。g<1 让中间灰度更深，纯黑纯白不动。"""
    table = []
    for v in range(256):
        c = min(1.0, (255 - v) / SPAN)
        table.append(max(0, min(255, int(round(255 - (c ** g) * SPAN)))))
    table[255] = 255          # 背景与 alpha 通道必须不动
    return bytes(table)


class Row(QWidget):
    """一行文字。gamma != 1 时走「离屏渲染 → LUT → 贴图」。"""

    def __init__(self, text, family, px, gamma, parent=None):
        super().__init__(parent)
        self.text = text
        self.family = family
        self.px = px
        self.gamma = gamma
        self.setFixedHeight(int(px * 2.2))

    def paintEvent(self, _e):
        dpr = self.devicePixelRatioF()
        w, h = int(self.width() * dpr), int(self.height() * dpr)
        img = QImage(w, h, QImage.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        f = QFont(self.family)
        f.setPixelSize(int(self.px * dpr))
        f.setStyleStrategy(QFont.NoSubpixelAntialias)
        p.setFont(f)
        p.setPen(QColor(INK_RGB, INK_RGB, INK_RGB))
        p.drawText(QRect(0, 0, w, h),
                   Qt.AlignLeft | Qt.AlignVCenter, self.text)
        p.end()

        if abs(self.gamma - 1.0) > 1e-6:
            # 原地改写。注意不能 QImage(bytes, ...)，那样 QImage 不接管内存，
            # 临时 bytes 一回收就是 access violation。
            lut = gamma_lut(self.gamma)
            buf = img.bits()
            buf[:] = bytes(buf).translate(lut)

        img.setDevicePixelRatio(dpr)
        q = QPainter(self)
        q.drawImage(0, 0, img)
        q.end()


CASES = [
    ("Microsoft YaHei UI", 1.0),
    ("Microsoft YaHei UI", 0.70),
    ("Microsoft YaHei UI", 0.50),
    ("HarmonyOS Sans SC", 1.0),
    ("HarmonyOS Sans SC", 0.70),
    ("HarmonyOS Sans SC", 0.50),
    ("思源黑体 CN Regular", 1.0),
    ("思源黑体 CN Regular", 0.70),
    ("思源黑体 CN Regular", 0.50),
]

PX = 18


class Board(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(pal.ColorRole.Window, QColor("white"))
        self.setPalette(pal)
        self.rows = []
        self.labels = []
        y = 8
        for fam, g in CASES:
            r = Row(TEXT, fam, PX, g, self)
            r.move(16, y)
            r.resize(760, r.height())
            r.show()
            self.rows.append(r)
            y += r.height()
        self.resize(800, y + 8)


def ink_stats(img: QImage):
    levels = [0] * 8
    total = 0
    for yy in range(img.height()):
        for xx in range(img.width()):
            c = img.pixelColor(xx, yy)
            v = (c.red() + c.green() + c.blue()) // 3
            if v < 252:
                levels[v // 32] += 1
                total += 1
    solid = sum(levels[:2])
    mid = sum(levels[2:6])
    faint = sum(levels[6:])
    return total, solid, mid, faint


def main():
    app = QApplication.instance() or QApplication([])
    b = Board()
    b.move(20, 20)
    b.show()

    def shot():
        for _ in range(8):
            app.processEvents()
        time.sleep(0.35)
        app.processEvents()
        scr = app.primaryScreen()
        dpr = scr.devicePixelRatio()
        g = b.frameGeometry()
        pm = scr.grabWindow(0).copy(QRect(int(g.x() * dpr), int(g.y() * dpr),
                                          int(g.width() * dpr),
                                          int(g.height() * dpr)))
        img = pm.toImage()
        img.save(str(OUT / "board_raw.png"))

        # 逐行裁切 + 统计
        print(f"{'字体':26s} {'gamma':>5s}  {'实心%':>6s} {'中间%':>6s} {'浅边%':>6s}")
        for i, (fam, gm) in enumerate(CASES):
            r = b.rows[i]
            tl = r.mapToGlobal(r.rect().topLeft())
            rect = QRect(int((tl.x() - g.x()) * dpr + 8),
                         int((tl.y() - g.y()) * dpr),
                         int(r.width() * dpr) - 16, int(r.height() * dpr))
            sub = img.copy(rect)
            tot, s, m, f = ink_stats(sub)
            print(f"{fam:26s} {gm:5.2f}  {100*s/max(1,tot):6.1f} "
                  f"{100*m/max(1,tot):6.1f} {100*f/max(1,tot):6.1f}")
            sub.scaled(sub.width() * 3, sub.height() * 3,
                       Qt.IgnoreAspectRatio,
                       Qt.FastTransformation).save(
                str(OUT / f"row_{i}_{fam[:6]}_{gm}.png"))
        b.close()
        app.quit()

    QTimer.singleShot(900, shot)
    app.exec()


if __name__ == "__main__":
    main()
