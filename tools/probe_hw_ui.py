"""截「热词」卡片：多行输入框 + 容量提示 + 清理按钮。

造一份**临时**命中统计（2 个命中过、9 个超期从未命中），截完删掉，
不碰真实数据；config.json 也原样还原。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src import hotwords as _hw  # noqa: E402

CFG = ROOT / "config.json"
BAK = ROOT / "logs" / "_cfg_backup.json"
TMP = ROOT / "logs" / "_hw_ui_data"
WORDS = "skill,github,markdown,压疮,鼻饲,翻身扣背,陪护,护理,压床,轮胎,月嫂"


class Stub:
    def reload_hotkey(self): pass
    def reload_asr(self): pass
    def apply_polish_config(self): pass


def seed_hits() -> None:
    """把模块的数据目录临时指到 logs/ 下，再造一份统计。"""
    TMP.mkdir(parents=True, exist_ok=True)
    _hw.HOTWORD_DIR = TMP
    _hw.HITS_PATH = TMP / "hits.json"
    _hw._hits_cache = None
    now = int(time.time())
    old = now - 6 * 86400
    data = {w: {"n": 0, "first": old, "last": 0}
            for w in WORDS.split(",")}
    data["鼻饲"] = {"n": 7, "first": old, "last": now}
    data["陪护"] = {"n": 3, "first": old, "last": now}
    _hw.HITS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    shutil.copy2(CFG, BAK)
    seed_hits()
    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        from src.ui.settings import SettingsWindow

        win = SettingsWindow(Stub())
        win.resize(820, 1000)
        win.show()
        app.processEvents()

        idx = next(i for i in range(win.nav.count())
                   if win.nav.item(i).text() == "文本")
        win.nav.setCurrentRow(idx)
        app.processEvents()

        win.ed_hw.setPlainText(WORDS)
        app.processEvents()
        print("粘贴逗号串后行数:", win.ed_hw.toPlainText().count("\n") + 1)

        win._normalize_hw()                    # 模拟失焦
        app.processEvents()
        lines = win.ed_hw.toPlainText().split("\n")
        print("失焦整理后行数:", len(lines), lines[:4], "...")
        print("容量提示:", win.lbl_hw_cnt.text())
        print("清理按钮可见:", win.btn_hw_clean.isVisible())
        print("可清理:", _hw.never_hit(_hw.manual_list(win.ed_hw.toPlainText())))

        # 诊断：输入框往上的每一层实际高度 vs sizeHint
        w = win.ed_hw
        while w is not None and w is not win:
            print(f"    {type(w).__name__:16} h={w.height():4} hint={w.sizeHint().height():4}")
            w = w.parentWidget()

        def shoot():
            win.grab().save(str(ROOT / "logs" / "hw_ui.png"))
            print("saved hw_ui.png（11 个词，高:", win.ed_hw.height(), "px）")
            win.ed_hw.setPlainText("skill\ngithub\nmarkdown")
            app.processEvents()
            win.grab().save(str(ROOT / "logs" / "hw_ui_short.png"))
            print("saved hw_ui_short.png（3 个词，高:", win.ed_hw.height(), "px）")
            app.quit()

        QTimer.singleShot(700, shoot)
        app.exec()
    finally:
        shutil.copy2(BAK, CFG)
        shutil.rmtree(TMP, ignore_errors=True)
        print("config.json 已还原，临时统计已删")
    return 0


if __name__ == "__main__":
    sys.exit(main())
