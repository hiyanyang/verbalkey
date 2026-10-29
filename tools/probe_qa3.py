"""第三轮：D 版 prompt + 更全的问句示例，扩到 12 条用例。

用法：python tools/probe_qa3.py
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
    "上海的首都是哪里那边的医院条件好不好",
    "陪护一天多少钱能不能按小时算",
    "这个药一天吃几次然后饭前还是饭后",
    "为什么老人晚上老是不睡觉",
    "我父亲这种情况需要请护工吗",
    "一百二十平的房子简单装修大概要多少钱",
    "医院在哪个位置我明天上午过去",
    "嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
    "我们这个服务主要是照顾刚出院的老人家属最关心人靠不靠得住",
]

SYS_D = ("你是语音转写的排版工具，不是助手，不回答问题。"
         + _PROMPTS[2] + "原文里如果有问句，一个字都不要替它回答，保持问句形态。")

# 三组示例：普通问句 / 「A的B在哪里」这种最容易被先验补全的 / 多问连说
SHOTS_Q = [
    ("百度搜索的网址是什么", "百度搜索的网址是什么？"),
    ("上海的首都是哪里那边的医院条件好不好",
     "上海的首都是哪里？那边的医院条件好不好？"),
    ("这个陪护费用是怎么算的然后一天多少钱",
     "这个陪护费用是怎么算的？一天多少钱？"),
]


def ask(eng, system, shots, c, temperature=0.15):
    msgs = [{"role": "system", "content": system}]
    for u, a in shots:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": c})
    body = json.dumps({
        "model": "local", "messages": msgs, "temperature": temperature,
        "max_tokens": max(96, min(len(c) * 2 + 48, 640)), "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(f"{eng.base}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with _LOCAL.open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"<ERR {e}>"


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
        shots = _SHOTS + SHOTS_Q
        for c in CASES:
            out = ask(eng, SYS_D, shots, c)
            print(f"IN : {c}\nOUT: {out}\n")
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
