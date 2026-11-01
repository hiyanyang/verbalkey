"""截「热词」卡片在超限（>8 个）状态下的样子，验证容量提示。

改完把 config.json 原样还原 —— 探针不能污染用户配置。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

CFG = ROOT / "config.json"
BAK = ROOT / "logs" / "_cfg_backup.json"
WORDS = "skill,github,markdown,压疮,鼻饲,翻身扣背,陪护,护理,压床,轮胎,月嫂"


class Stub:
    def reload_hotkey(self): pass
    def reload_asr(self): pass
    def apply_polish_config(self): pass


def main() -> int:
    shutil.copy2(CFG, BAK)
    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        from src.ui.settings import SettingsWindow

        win = SettingsWindow(Stub())
        win.resize(780, 900)
        win.show()
        app.processEvents()

        idx = next(i for i in range(win.nav.count())
                   if win.nav.item(i).text() == "文本")
        win.nav.setCurrentRow(idx)
        app.processEvents()
        win.ed_hw.setText(WORDS)
        app.processEvents()
        print("标签文字:", win.lbl_hw_cnt.text())

        def shoot():
            win.grab().save(str(ROOT / "logs" / "hw_ui.png"))
            print("saved logs/hw_ui.png")
            app.quit()

        QTimer.singleShot(600, shoot)
        app.exec()
    finally:
        shutil.copy2(BAK, CFG)
        print("config.json 已还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
