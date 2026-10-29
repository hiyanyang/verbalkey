"""第四轮：示例数量 vs 耗时/准确率，找性价比点。

用法：python tools/probe_qa4.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import _cfg_guard  # noqa: E402

_cfg_guard.protect()

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine, _PROMPTS, _question_lost  # noqa: E402

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

S1 = ("嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
      "我今天上午去了一趟医院，医生说恢复得还行，就是需要人照顾。")
S2 = ("我母亲刚出院现在在家需要有人照顾但是白天我们都要上班",
      "我母亲刚出院，现在在家需要有人照顾。但白天我们都要上班。")
S3 = ("我想问一下陪护一天多少钱然后能不能按小时算",
      "我想问一下陪护一天多少钱，能不能按小时算？")
S4 = ("嗯我们这个服务主要是照顾刚出院的老人家属最关心人靠不靠得住",
      "我们这个服务主要是照顾刚出院的老人，家属最关心人靠不靠得住。")
QQ1 = ("百度搜索的网址是什么", "百度搜索的网址是什么？")
QQ2 = ("上海的首都是哪里那边的医院条件好不好",
       "上海的首都是哪里？那边的医院条件好不好？")
QQ3 = ("这个陪护费用是怎么算的然后一天多少钱",
       "这个陪护费用是怎么算的？一天多少钱？")
QQ4 = ("为什么老人晚上老是不睡觉", "为什么老人晚上老是不睡觉？")

CASES = [
    "百度搜索的网址是什么",
    "北京的首都是哪里然后那边的陪护贵不贵",
    "为什么老人晚上老是不睡觉",
    "上海的首都是哪里那边的医院条件好不好",
    "陪护一天多少钱能不能按小时算",
    "嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
]


def ask(eng, system, shots, c):
    msgs = [{"role": "system", "content": system}]
    for u, a in shots:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": c})
    body = json.dumps({
        "model": "local", "messages": msgs, "temperature": 0.15,
        "max_tokens": max(96, min(len(c) * 2 + 48, 640)), "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(f"{eng.base}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _LOCAL.open(req, timeout=120) as r:
        out = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    return out.strip(), time.time() - t0


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
    sys_p = _PROMPTS[2]
    variants = [
        ("4条(原)", [S1, S2, S3, S4]),
        ("6条(+2问句)", [S1, S2, S3, S4, QQ1, QQ2]),
        ("7条(+3问句)", [S1, S2, S3, S4, QQ1, QQ2, QQ3]),
    ]
    try:
        for tag, shots in variants:
            print("\n" + "=" * 58)
            print(tag)
            print("=" * 58)
            ts, bad = [], 0
            for c in CASES:
                out, dt = ask(eng, sys_p, shots, c)
                ts.append(dt)
                miss = _question_lost(c, out)
                bad += 1 if miss else 0
                print(f"[{dt:4.1f}s]{' 答题!' if miss else '      '} {c}")
                print(f"      -> {out}")
            print(f"均值 {sum(ts)/len(ts):.2f}s  被答题 {bad}/{len(CASES)}")
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
