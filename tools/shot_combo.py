"""截取下拉框展开态和滑杆，检查圆角面板有没有多余的直角方框、手柄是否完整。

弹出层是独立的顶层窗口，win.grab() 抓不到，必须抓屏幕再裁。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QRect, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs" / "ctrl"
OUT.mkdir(parents=True, exist_ok=True)


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    from src.ui.settings import SettingsWindow

    class S:
        def reload_hotkey(self): pass
        def reload_asr(self): pass
        def apply_polish_config(self): pass

    w = SettingsWindow(S())
    w.resize(880, 760)
    w.show()
    w.raise_()
    w.activateWindow()
    app.processEvents()

    def step1():
        w.nav.setCurrentRow(2)
        app.processEvents()
        pm = w.sl_silence.grab()
        pm = pm.scaled(pm.width() * 3, pm.height() * 3)
        pm.save(str(OUT / "slider.png"))
        w.cb_dev._open()
        QTimer.singleShot(500, step2)

    def step2():
        app.processEvents()
        pop = w.cb_dev._popup
        if not pop or not pop.isVisible():
            print("弹出层没打开")
            app.quit()
            return
        # 把控件四周留 24px 一起截进来，方便看有没有多余的方框边
        extra = 24
        g = pop.geometry()
        rect = QRect(g.x() - extra, g.y() - extra,
                     g.width() + extra * 2, g.height() + extra * 2)
        scr = app.screenAt(pop.pos()) or app.primaryScreen()
        ratio = scr.devicePixelRatio()
        shot = scr.grabWindow(0)
        shot = shot.copy(QRect(int(rect.x() * ratio), int(rect.y() * ratio),
                               int(rect.width() * ratio),
                               int(rect.height() * ratio)))
        shot.setDevicePixelRatio(ratio)
        shot.save(str(OUT / "combo_popup.png"))
        print("saved", OUT / "combo_popup.png", "dpr=", ratio)
        pop.close()
        app.quit()

    QTimer.singleShot(500, step1)
    app.exec()


if __name__ == "__main__":
    main()
