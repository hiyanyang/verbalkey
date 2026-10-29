"""冒烟测试：渲染界面截图 + 检查音频设备。不阻塞，跑完自动退出。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs"


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    from src.ui.hud import Hud
    from src.ui.settings import SettingsWindow

    class Stub:
        def reload_hotkey(self): pass
        def reload_asr(self): pass
        def apply_polish_config(self): pass

    win = SettingsWindow(Stub())
    win.show()
    QTimer.singleShot(400, lambda: win.grab().save(str(OUT / "shot_settings.png")))

    hud = Hud()
    hud.show_hud("recording", "今天下午三点把王大爷从医院接回家，记得带上他的降压药。")
    hud.set_hint("按 右 Ctrl，或点下面「用整理版」，替换刚写出的原文")
    QTimer.singleShot(900, lambda: hud.grab().save(str(OUT / "shot_hud.png")))

    def devices():
        try:
            from src.audio import Recorder
            ds = Recorder.list_devices()
            print("输入设备：")
            for i, n in ds:
                print(f"  [{i}] {n}")
        except Exception as e:
            print("设备枚举失败:", e)

    QTimer.singleShot(1000, devices)
    QTimer.singleShot(1400, app.quit)
    app.exec()


if __name__ == "__main__":
    main()
