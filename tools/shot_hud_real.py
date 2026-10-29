"""抓真实屏幕（不是 widget.grab()）检查浮窗，因为 grab() 走的是带 alpha 的离屏
表面，永远拿不到 ClearType，看不出真实观感。

顺带统计文字边缘的彩色子像素比例：大于 0 说明 ClearType 生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QRect, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / "logs" / "hud"
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE = ("我这边是做陪护的，主要派护理员上门照顾刚出院的老人。"
          "很多家属第一次找我们，最关心的就是人靠不靠得住。")


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    from src.ui.hud import Hud

    h = Hud()
    h.show()
    h.show_hud("done", SAMPLE)
    h.set_hint("按 右 Ctrl，或点下面「用整理版」，替换刚写出的原文")
    h.show_actions(lambda: None, lambda: None)

    def shoot():
        app.processEvents()
        scr = app.primaryScreen()
        ratio = scr.devicePixelRatio()
        g = h.frameGeometry()
        shot = scr.grabWindow(0)
        shot = shot.copy(QRect(int(g.x() * ratio), int(g.y() * ratio),
                               int((g.width() + 2) * ratio),
                               int((g.height() + 2) * ratio)))
        shot.setDevicePixelRatio(ratio)
        shot.save(str(OUT / "real_screen.png"))

        # 统计正文区域的彩边比例
        img = shot.toImage()
        colored = total = 0
        y0 = int(h.text.geometry().y() * ratio)
        y1 = int((h.text.geometry().y() + h.text.geometry().height()) * ratio)
        for y in range(y0, min(y1, img.height())):
            for x in range(0, img.width()):
                c = img.pixelColor(x, y)
                r, g_, b = c.red(), c.green(), c.blue()
                if (r, g_, b) != (255, 255, 255):
                    total += 1
                    if abs(r - g_) > 8 or abs(g_ - b) > 8 or abs(r - b) > 8:
                        colored += 1
        pct = 100.0 * colored / max(1, total)
        print(f"正文着色像素 {total}，彩边 {colored}（{pct:.1f}%）")
        print("ClearType:", "生效" if pct > 5 else "未生效（还是灰度抗锯齿）")
        print("DWM 圆角:", "生效" if h._dwm_ok else "未生效，用的是遮罩")
        print("saved", OUT / "real_screen.png")
        h.hide()
        app.quit()

    QTimer.singleShot(600, shoot)
    app.exec()


if __name__ == "__main__":
    main()
