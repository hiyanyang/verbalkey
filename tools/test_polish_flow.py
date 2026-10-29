"""整理结果的两条路径自测：先上屏原文再确认替换 / 直接等整理版。

不碰真实键盘鼠标：replace_last 和 _paste 都换成立即返回的假函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _cfg_guard  # noqa: E402
_cfg_guard.protect()
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from PySide6.QtWidgets import QApplication  # noqa: E402

import src.main as m  # noqa: E402
from src.config import CONFIG, CONFIG_PATH  # noqa: E402

FAILS = []


def check(name: str, cond: bool, extra: str = ""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run() -> int:
    m.App._load_asr_async = lambda self: None
    m.App._start_hotkeys = lambda self: None

    qt = QApplication([])
    app = m.App(qt)

    pasted: list[str] = []
    replaced: list[tuple[str, str]] = []
    app._paste = lambda t: (pasted.append(t), "sent")[1]
    app._polish_async = lambda text, level, seq: None     # 手动喂结果，不起线程
    m.replace_last = lambda old, new, **kw: replaced.append((old, new))

    CONFIG.data["polish"]["enabled"] = True
    CONFIG.data["polish"]["level"] = 2
    CONFIG.data["polish"]["auto_replace"] = False
    CONFIG.data["polish"]["paste_mode"] = "raw"

    # --- 路径 A：先上屏原文，整理完等确认键替换 ---
    app.buffer = ["第一段原文"]
    app._on_finish()
    check("A1 原文先上屏", pasted == ["第一段原文"], str(pasted))
    check("A2 上屏后没有待替换", app.pending_replace is None)

    app._on_polished("第一段，整理版。", "")
    check("A3 整理完挂起待替换", app.pending_replace is not None)
    check("A4 浮窗显示动作按钮", app.hud.actions.isVisible())

    app._confirm()
    check("A5 确认键触发替换", replaced == [("第一段原文", "第一段，整理版。")], str(replaced))
    check("A6 替换后清空待替换", app.pending_replace is None)
    check("A7 last_pasted 更新", app.last_pasted == "第一段，整理版。")

    # --- 路径 A'：忽略 ---
    app.buffer = ["第二段原文"]
    app._on_finish()
    app._on_polished("第二段整理版", "")
    app._ignore()
    check("A8 忽略后不再待替换", app.pending_replace is None)

    # --- 路径 A''：45 秒后确认键失效 ---
    app.buffer = ["第三段原文"]
    app._on_finish()
    app._on_polished("第三段整理版", "")
    text, _exp = app.pending_replace
    app.pending_replace = (text, 0)      # 手动过期
    before = len(replaced)
    app._confirm()
    check("A9 过期后确认键不动作", len(replaced) == before)

    # --- 路径 B：直接等整理版上屏 ---
    CONFIG.data["polish"]["paste_mode"] = "polished"
    pasted.clear()
    app.buffer = ["第四段原文"]
    app._on_finish()
    check("B1 直出模式先不上屏", pasted == [], str(pasted))
    app._on_polished("第四段，整理版。", "")
    check("B2 整理完一次上屏", pasted == ["第四段，整理版。"], str(pasted))
    check("B3 不再需要确认", app.pending_replace is None)

    # --- 路径 B'：整理失败要退回原文 ---
    pasted.clear()
    app.buffer = ["第五段原文"]
    app._on_finish()
    app._on_polished("", "模型没起来")
    check("B4 整理失败补原文", pasted == ["第五段原文"], str(pasted))

    # --- 序号对不上的旧结果必须丢掉（连环口述时会发生）---
    pasted.clear()
    app.buffer = ["第六段原文"]
    app._on_finish()
    app._on_polished("第六段，整理版。", "", 999)
    check("D1 过期结果被丢弃", pasted == [], str(pasted))
    app._on_polished("第六段，整理版。", "")
    check("D2 当前结果才上屏", pasted == ["第六段，整理版。"], str(pasted))

    # --- 路径 E：去口水词走本地规则，同步完成，不留待替换 ---
    CONFIG.data["polish"]["level"] = 1
    CONFIG.data["polish"]["paste_mode"] = "raw"
    pasted.clear()
    app.buffer = ["嗯那个我想问一下价格"]
    app._on_finish()
    check("E1 L1 直接上屏清理后的文本", pasted == ["我想问一下价格"], str(pasted))
    check("E2 L1 不留待替换", app.pending_replace is None)

    # --- 确认键和录音键撞车时不注册 ---
    CONFIG.data["hotkey"]["vk"] = 0xA5
    CONFIG.data["hotkey"]["confirm_vk"] = 0xA5
    app._start_confirm_key()
    check("C1 撞键时跳过注册", app.confirm_key is None)

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 1 if FAILS else 0


def main() -> int:
    """跑测试前备份用户配置，跑完原样写回。

    以前直接在 CONFIG.data 上改，某一步触发 CONFIG.save() 就把
    测试用的 level=1、撞车用的 hotkey=0xA5 写进了真实 config.json，
    用户重启后整理强度被悄悄改掉，还以为开关坏了。
    """
    backup = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
    try:
        return run()
    finally:
        if backup is not None:
            CONFIG_PATH.write_text(backup, encoding="utf-8")
            print("config.json 已还原")


if __name__ == "__main__":
    sys.exit(main())
