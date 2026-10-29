"""复现：L2 书面化会不会把口述里的问句直接回答掉。

用法：python tools/probe_qa.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import _cfg_guard  # noqa: E402

_cfg_guard.protect()

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine  # noqa: E402

CASES = [
    "百度搜索的网址是什么",
    "北京的首都是哪里然后那边的陪护贵不贵",
    "嗯我就想问一下这个陪护的费用是怎么算的",
    "这个老人的血压多少算正常",
    "我一米七五体重八十公斤算不算胖",
    "上海的首都是哪里那边的医院条件好不好",
    "陪护一天多少钱能不能按小时算",
    "这个药一天吃几次然后饭前还是饭后",
    "为什么老人晚上老是不睡觉",
    "我父亲这种情况需要请护工吗",
    "一百二十平的房子简单装修大概要多少钱",
    "医院在哪个位置我明天上午过去",
    "这样安排对不对",
    "我也不知道为什么",
    "嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
    "我们这个服务主要是照顾刚出院的老人家属最关心人靠不靠得住",
]


def main() -> int:
    eng = PolishEngine()
    eng.configure(
        CONFIG.get("polish", "server_bin") or "",
        CONFIG.get("polish", "model_file") or "",
        int(CONFIG.get("polish", "port") or 8080),
        float(CONFIG.get("polish", "idle_unload_min") or 5),
    )
    ok, err = eng.ensure()
    print("engine:", ok, err)
    if not ok:
        return 1
    try:
        for c in CASES:
            out, e = eng.polish(c, 2)
            print("\nIN :", c)
            print("OUT:", out, ("  [%s]" % e) if e else "")
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
