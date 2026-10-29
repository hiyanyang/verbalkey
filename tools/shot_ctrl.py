"""把关键控件单独放大截图，用来确认细节（箭头、开关、勾）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs" / "ctrl"
OUT.mkdir(parents=True, exist_ok=True)


class Stub:
    def reload_hotkey(self): pass
    def reload_asr(self): pass
    def apply_polish_config(self): pass


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from src.ui.settings import SettingsWindow

    win = SettingsWindow(Stub())
    win.resize(780, 620)
    win.show()
    app.processEvents()

    def big(w, name, scale=3):
        pm = w.grab()
        pm = pm.scaled(pm.width() * scale, pm.height() * scale)
        p = OUT / f"{name}.png"
        pm.save(str(p))
        print("saved", p)

    def shoot():
        # 通用页
        win.nav.setCurrentRow(0)
        app.processEvents()
        big(win.cb_hud, "combo_short")
        big(win.cb_paste, "combo_long")
        big(win.sw_autostart, "switch_off")
        win.sw_autostart.setChecked(True)
        app.processEvents()
        big(win.sw_autostart, "switch_on")
        # 口述整理页
        win.nav.setCurrentRow(4)
        app.processEvents()
        big(win.grp_level, "option_group")
        # 识别模型页
        win.nav.setCurrentRow(5)
        app.processEvents()
        big(win.cb_model, "combo_model")

    QTimer.singleShot(400, shoot)
    QTimer.singleShot(1600, app.quit)
    app.exec()


if __name__ == "__main__":
    main()
