"""浮窗三种状态截图：录音中（实时文字）、整理中、整理完成带按钮。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs" / "hud"
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE = ("我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"
          "很多家属第一次找我们，最关心的就是人靠不靠得住。")


def on_bg(widget, name, bg="#E8E8ED", scale=2):
    """贴一个背景色再截图，否则透明圆角在查看器里是黑底。"""
    pm = widget.grab()
    if scale != 1:
        pm = pm.scaled(pm.width() * scale, pm.height() * scale)
    img = QImage(pm.size(), QImage.Format_ARGB32)
    img.fill(QColor(bg))
    p = QPainter(img)
    p.drawPixmap(0, 0, pm)
    p.end()
    img.save(str(OUT / name))


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    from src.ui.hud import Hud

    h = Hud()
    h.show()

    h.show_hud("recording", SAMPLE[:30])
    app.processEvents()
    on_bg(h, "1_recording.png")

    h.set_state("polishing")
    h.set_text(SAMPLE)
    h.set_hint("整理好会直接写进光标，稍等一下")
    app.processEvents()
    on_bg(h, "2_polishing.png")

    h.show_hud("done", SAMPLE)
    h.set_hint("按 右 Ctrl，或点下面「用整理版」，替换刚写出的原文")
    h.show_actions(lambda: None, lambda: None)
    app.processEvents()
    on_bg(h, "3_done_actions.png")

    print("saved to", OUT)
    h.hide()
    app.quit()


if __name__ == "__main__":
    main()
