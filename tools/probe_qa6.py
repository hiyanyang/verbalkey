"""第六轮：定界符包裹到底能省掉多少 few-shot。每个变体跑 3 轮，排除偶然。

用法：python tools/probe_qa6.py
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
from src.polish import PolishEngine, _PROMPTS, _SHOTS, _question_lost, _pronoun_lost  # noqa: E402
from tools.probe_qa5 import CASES, SYS_G, ask  # noqa: E402

S = {i + 1: s for i, s in enumerate(_SHOTS)}
# S[1]医院 S[2]母亲 S[3]陪护多少钱(问) S[4]你们这个服务 S[5]百度 S[6]上海 S[7]费用(问) S[8]为什么(问)
V_LEAN = [S[1], S[2], S[4]]              # 三条纯陈述，零问句示例
V_LEAN_Q = [S[1], S[2], S[4], S[8]]      # 加一条「为什么」问句
V_MID = [S[1], S[2], S[3], S[4], S[7], S[8]]  # 上一轮 D 用的 6 条
V_FULL = _SHOTS


def run(eng, tag, system, shots, wrap, rounds=3):
    fails, ts = [], []
    for _ in range(rounds):
        for c in CASES:
            out, dt = ask(eng, system, shots, c, wrap)
            ts.append(dt)
            v = []
            if _question_lost(c, out):
                v.append("答题")
            if _pronoun_lost(c, out):
                v.append("改人称")
            if out.startswith("<") or "<文本>" in out:
                v.append("漏标签")
            if v:
                fails.append((c, out, ",".join(v)))
    total = len(CASES) * rounds
    print(f"\n{tag}（{len(shots)} 例, {rounds} 轮）")
    print(f"  失败 {len(fails)}/{total}   均值 {sum(ts)/len(ts):.2f}s")
    for c, out, v in fails[:6]:
        print(f"   ✗ {v} | {c[:28]}\n       -> {out[:70]}")


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
        run(eng, "1. 业界system + 3例 + 包裹", SYS_G, V_LEAN, True)
        run(eng, "2. 业界system + 4例(1问) + 包裹", SYS_G, V_LEAN_Q, True)
        run(eng, "3. 业界system + 6例 + 包裹", SYS_G, V_MID, True)
        run(eng, "4. 现行system + 8例 + 包裹", _PROMPTS[2], V_FULL, True)
        run(eng, "5. 业界system + 4例 + 无包裹", SYS_G, V_LEAN_Q, False)
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
