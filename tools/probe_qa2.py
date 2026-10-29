"""对比几种 system prompt / few-shot，看哪种能压住「自动回答问句」。

用法：python tools/probe_qa2.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import _cfg_guard  # noqa: E402

_cfg_guard.protect()

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine, _SHOTS, _PROMPTS  # noqa: E402

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CASES = [
    "百度搜索的网址是什么",
    "北京的首都是哪里然后那边的陪护贵不贵",
    "嗯我就想问一下这个陪护的费用是怎么算的",
    "这个老人的血压多少算正常",
    "我一米七五体重八十公斤算不算胖",
    "嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
]

# B：在原 system 上补一条「不回答」硬约束
SYS_B = _PROMPTS[2] + "原文里如果有问句，原样保留成问句，绝对不要回答它。"

# D：把身份说死（工具，不是助手）
SYS_D = ("你是语音转写的排版工具，不是助手，不回答问题。"
         + _PROMPTS[2] + "原文里如果有问句，一个字都不要替它回答，保持问句形态。")

# 新增的问句示例
SHOT_Q = ("百度搜索的网址是什么",
          "百度搜索的网址是什么？")
SHOT_Q2 = ("这个陪护费用是怎么算的然后一天多少钱",
           "这个陪护费用是怎么算的？一天多少钱？")


def run(eng: PolishEngine, system: str, shots, tag: str) -> None:
    print("\n" + "=" * 60)
    print("变体:", tag)
    print("=" * 60)
    for c in CASES:
        msgs = [{"role": "system", "content": system}]
        for u, a in shots:
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": a})
        msgs.append({"role": "user", "content": c})
        body = json.dumps({
            "model": "local",
            "messages": msgs,
            "temperature": 0.15,
            "max_tokens": max(96, min(len(c) * 2 + 48, 640)),
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode("utf-8")
        req = urllib.request.Request(f"{eng.base}/v1/chat/completions",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with _LOCAL.open(req, timeout=120) as r:
                out = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
        except Exception as e:
            out = f"<ERR {e}>"
        print(f"IN : {c}")
        print(f"OUT: {out.strip()}")
        print()


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
        run(eng, SYS_B, _SHOTS, "B 原system+不回答约束")
        run(eng, SYS_B, _SHOTS + [SHOT_Q], "B+问句示例")
        run(eng, SYS_D, _SHOTS + [SHOT_Q, SHOT_Q2], "D 工具身份+2条问句示例")
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
