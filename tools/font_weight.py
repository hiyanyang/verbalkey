"""同一句话在四种字号/字重下的真实观感对比。

窗口不透明，真机抓屏；放大 2 倍是为了看清边缘结构。
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

SAMPLE = "我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"

CASES = [
    ("A 16px Regular（当前）", 16, QFont.Normal, QFont.PreferDefaultHinting),
    ("B 18px Regular", 18, QFont.Normal, QFont.PreferDefaultHinting),
    ("C 18px Bold", 18, QFont.Bold, QFont.PreferDefaultHinting),
    ("D 16px Bold", 16, QFont.Bold, QFont.PreferDefaultHinting),
    ("E 20px Regular", 20, QFont.Normal, QFont.PreferDefaultHinting),
]


def hud_font(px: int, weight, hinting) -> QFont:
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(hinting)
    f.setStyleStrategy(QFont.NoSubpixelAntialias)
    return f


def main():
    app = QApplication([])
    app.setStyle("Fusion")

    win = QWidget()
    win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    win.setAutoFillBackground(True)
    pal = win.palette()
    pal.setColor(QPalette.Window, QColor("white"))
    win.setPalette(pal)
    win.setFixedWidth(600)

    lay = QVBoxLayout(win)
    lay.setContentsMargins(16, 12, 16, 12)
    lay.setSpacing(4)

    bodies = []
    for name, px, weight, hinting in CASES:
        tag = QLabel(name)
        tf = QFont("Microsoft YaHei UI")
        tf.setPixelSize(10)
        tf.setStyleStrategy(QFont.NoSubpixelAntialias)
        tag.setFont(tf)
        tag.setStyleSheet("color:#a1a1a6;")
        lay.addWidget(tag)

        body = QLabel(SAMPLE)
        body.setFont(hud_font(px, weight, hinting))
        body.setStyleSheet("color:#1D1D1F;")
        lay.addWidget(body)
        bodies.append(body)

    win.adjustSize()
    scr = QApplication.primaryScreen()
    g = scr.availableGeometry()
    win.move(g.x() + 60, g.y() + 60)
    win.show()

    def shoot():
        app.processEvents()
        ratio = scr.devicePixelRatio()
        fg = win.frameGeometry()
        shot = scr.grabWindow(0).copy(
            QRect(int(fg.x() * ratio), int(fg.y() * ratio),
                  int(fg.width() * ratio), int(fg.height() * ratio)))
        img = shot.toImage()
        img.save(str(OUT / "weight_native.png"))
        big = img.scaled(img.width() * 2, img.height() * 2,
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big.save(str(OUT / "weight_2x.png"))
        print("saved", OUT / "weight_2x.png", img.width(), img.height())
        win.close()
        app.quit()

    QTimer.singleShot(700, shoot)
    app.exec()


if __name__ == "__main__":
    main()
