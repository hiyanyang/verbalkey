"""字体像素级对比：只放六个字，裁出来放大 6 倍，看得清边缘到底是彩边还是灰阶。

四组：现状（ClearType）/ 16px 灰度 / 16px 灰度+全hint / 17px 灰度
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

OUT = ROOT / "logs" / "font"
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE = "我这边是做陪护"

CASES = [
    ("现状 15px ClearType", 15, False, QFont.PreferDefaultHinting),
    ("16px 灰度", 16, True, QFont.PreferDefaultHinting),
    ("16px 灰度+全hint", 16, True, QFont.PreferFullHinting),
    ("17px 灰度+全hint", 17, True, QFont.PreferFullHinting),
]


def main():
    app = QApplication([])
    app.setStyle("Fusion")

    win = QWidget()
    win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    win.setAutoFillBackground(True)
    pal = win.palette()
    pal.setColor(QPalette.Window, QColor("white"))
    win.setPalette(pal)
    win.setFixedWidth(300)

    lay = QVBoxLayout(win)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(6)

    bodies = []
    for label_text, px, gray, hinting in CASES:
        tag = QLabel(label_text)
        f_tag = QFont("Microsoft YaHei UI")
        f_tag.setPixelSize(9)
        f_tag.setStyleStrategy(QFont.NoSubpixelAntialias)
        tag.setFont(f_tag)
        tag.setStyleSheet("color:#a1a1a6;")
        lay.addWidget(tag)

        body = QLabel(SAMPLE)
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(px)
        f.setHintingPreference(hinting)
        if gray:
            f.setStyleStrategy(QFont.NoSubpixelAntialias)
        body.setFont(f)
        body.setStyleSheet("color:#1D1D1F;")
        lay.addWidget(body)
        bodies.append((label_text, body))

    win.adjustSize()
    scr = QApplication.primaryScreen()
    g = scr.availableGeometry()
    win.move(g.x() + 40, g.y() + 40)
    win.show()

    def shoot():
        app.processEvents()
        ratio = scr.devicePixelRatio()
        fg = win.frameGeometry()
        shot = scr.grabWindow(0).copy(
            QRect(int(fg.x() * ratio), int(fg.y() * ratio),
                  int(fg.width() * ratio), int(fg.height() * ratio)))
        img = shot.toImage()

        # 每行正文裁出来，6 倍最近邻放大，竖向拼接
        rows = []
        for name, body in bodies:
            r = body.geometry()
            crop = img.copy(QRect(int((r.x() - 2) * ratio),
                                  int((r.y() - 2) * ratio),
                                  int((r.width() + 4) * ratio),
                                  int((r.height() + 4) * ratio)))
            rows.append(crop.scaled(crop.width() * 4, crop.height() * 4,
                                    Qt.IgnoreAspectRatio, Qt.FastTransformation))

        W = max(r.width() for r in rows)
        H = sum(r.height() + 8 for r in rows)
        out = QImage(W, H, QImage.Format_RGB32)
        out.fill(QColor("white"))
        p = QPainter(out)
        y = 0
        for r in rows:
            p.drawImage(0, y, r)
            y += r.height() + 8
        p.end()
        out.save(str(OUT / "pixel_4x.png"))
        print("saved", OUT / "pixel_4x.png", out.width(), out.height())
        win.close()
        app.quit()

    QTimer.singleShot(700, shoot)
    app.exec()


if __name__ == "__main__":
    main()
