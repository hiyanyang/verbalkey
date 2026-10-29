"""把设置界面每一页都截图，方便逐页检查控件渲染。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs" / "pages"
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
    win.resize(760, 620)
    win.show()
    app.processEvents()

    n = win.nav.count()

    def shoot(i):
        win.nav.setCurrentRow(i)
        app.processEvents()
        name = win.nav.item(i).text()
        path = OUT / f"{i}_{name}.png"
        win.grab().save(str(path))
        print("saved", path)

    for i in range(n):
        QTimer.singleShot(200 + i * 250, lambda i=i: shoot(i))
    QTimer.singleShot(300 + n * 250 + 400, app.quit)
    app.exec()


if __name__ == "__main__":
    main()
