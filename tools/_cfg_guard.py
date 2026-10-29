"""测试脚本的配置保护。

背景：`test_polish_flow` / `test_flow` / `test_modes` 会为了造场景去改
`CONFIG`（比如把整理强度设成 1、把热键改成撞车），而 `CONFIG.set()`
会立刻整份写回 `config.json`。跑一次测试，用户的真实设置就被覆盖一次 ——
表现为「刚在设置里调好，重启后又变回去了」，很难查。

用法：脚本里 `import _cfg_guard; _cfg_guard.protect()`，放在最早期。
用 atexit 兜底，脚本中途抛异常也能还原。
"""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def protect() -> None:
    # 第一道：直接关掉落盘。这是根治 —— 备份/还原只能兜「已经写进去」的情况，
    # 而备份本身可能是脏的（上一次测试写坏后留下的值会被当成原始配置）
    try:
        from src.config import CONFIG
        CONFIG.save_enabled = False
    except Exception:
        pass

    # 第二道：万一有别的地方绕过 CONFIG 直接写了文件，退出时还原
    if not CONFIG_PATH.exists():
        return
    try:
        backup = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception:
        return

    def restore():
        try:
            CONFIG_PATH.write_text(backup, encoding="utf-8")
            print("config.json 已还原（测试保护）")
        except Exception:
            pass

    atexit.register(restore)
