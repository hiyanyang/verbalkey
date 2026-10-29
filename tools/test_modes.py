"""验证两种按键模式：按住模式（按下开始/松开结束）、切换模式（按一下开始/再按一下结束）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _cfg_guard  # noqa: E402
_cfg_guard.protect()
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from tools.test_flow import SAMPLES, patch_sounddevice  # noqa: E402


def wait_ready(app, qt, limit=60):
    for _ in range(limit * 2):
        qt.processEvents()
        if app.asr.ready:
            return True
        time.sleep(0.5)
    return False


def pump(qt, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        qt.processEvents()
        time.sleep(0.03)


def main():
    patch_sounddevice()
    from PySide6.QtWidgets import QApplication
    from src.config import CONFIG

    qt = QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    qt.setStyle("Fusion")
    import src.main as m
    app = m.App(qt)
    if not wait_ready(app, qt):
        print("FAIL: ASR 未就绪")
        return 1

    # 自测时不要真的往光标里粘贴，也不起整理进程
    app._paste = lambda t: "sent"
    app._start_polish = lambda text, level, direct=False: None
    # 整理被跳过后，「直出模式」会等一个永远不会来的结果，看不到上屏，
    # 所以自测统一走「先上屏原文」这条路
    CONFIG.data["polish"]["paste_mode"] = "raw"
    CONFIG.data["polish"]["level"] = 0

    ok = True

    # ---- 按住模式 ----
    CONFIG.set("hotkey", "mode", "hold")
    app._on_press()
    print("按住模式 按下 -> busy =", app.busy)
    ok &= app.busy
    pump(qt, 6)
    app._on_release()
    print("按住模式 松开 -> 等待收尾")
    pump(qt, 25)
    print("   busy =", app.busy, " 上屏文本长度 =", len(app.last_pasted))
    ok &= (not app.busy) and len(app.last_pasted) > 0

    # ---- 切换模式 ----
    CONFIG.set("hotkey", "mode", "toggle")
    app.last_pasted = ""
    app._on_press()
    print("切换模式 第一次按下 -> busy =", app.busy)
    ok &= app.busy
    pump(qt, 6)
    app._on_release()          # 切换模式下松开不该结束
    print("切换模式 松开后   -> busy =", app.busy, "(应仍为 True)")
    ok &= app.busy
    pump(qt, 2)
    app._on_press()            # 再按一下才结束
    print("切换模式 第二次按下 -> 等待收尾")
    pump(qt, 25)
    print("   busy =", app.busy, " 上屏文本长度 =", len(app.last_pasted))
    ok &= (not app.busy) and len(app.last_pasted) > 0

    print("结果:", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
