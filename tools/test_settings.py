"""设置界面联动自测：翻遍每一页，拨动每一个控件，捕获任何异常。

跑完把 config.json 还原，避免自测把用户设置改掉。
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CONFIG, CONFIG_PATH  # noqa: E402
from src.ui.settings import SettingsWindow  # noqa: E402
from src.ui.widgets import Combo, Slider, Spin, Switch, OptionRow  # noqa: E402

calls = []


class Stub:
    def reload_hotkey(self):
        calls.append("reload_hotkey")

    def reload_asr(self):
        calls.append("reload_asr")

    def apply_polish_config(self):
        calls.append("apply_polish_config")


errors = []
BACKUP = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def run():
    app = QApplication([])
    app.setStyle("Fusion")
    w = SettingsWindow(Stub())
    w.resize(820, 660)
    w.show()
    app.processEvents()

    def poke(widget):
        """对每种控件做一次交互。"""
        try:
            if isinstance(widget, Switch):
                v = widget.isChecked()
                widget.setChecked(not v)
                widget.setChecked(v)
            elif isinstance(widget, OptionRow):
                if not widget.isChecked():
                    widget.setChecked(True)
            elif isinstance(widget, Combo):
                if widget.count() > 1:
                    cur = widget.currentIndex()
                    widget.setCurrentIndex(1)
                    if cur >= 0:
                        widget.setCurrentIndex(cur)
            elif isinstance(widget, Slider):
                v = widget.value()
                widget.setValue(min(widget.maximum(), v + 1))
                widget.setValue(v)
            elif isinstance(widget, Spin):
                v = widget.value()
                lo, hi = widget._min, widget._max
                widget.setValue(hi)
                widget.setValue(lo)
                widget.setValue(v)
            else:
                print(f"  未覆盖的控件类型: {type(widget).__name__}")
        except Exception:
            errors.append(f"{type(widget).__name__}: {traceback.format_exc()}")

    state = {"i": 0}

    def step():
        i = state["i"]
        if i >= w.nav.count():
            finish(app)
            return
        w.nav.setCurrentRow(i)
        app.processEvents()
        page = w.stack.widget(i)
        for child in page.findChildren(object):
            if isinstance(child, (Switch, Combo, Slider, Spin, OptionRow)):
                poke(child)
        app.processEvents()
        print(f"[{i}] {w.nav.item(i).text()}  OK")
        state["i"] += 1
        QTimer.singleShot(60, step)

    def finish(a):
        print("触发的回调:", calls)
        print("异常:", errors if errors else "无")
        a.quit()

    QTimer.singleShot(200, step)
    app.exec()

    if BACKUP is not None:
        CONFIG_PATH.write_text(BACKUP, encoding="utf-8")
        print("config.json 已还原")


if __name__ == "__main__":
    run()
    sys.exit(1 if errors else 0)
