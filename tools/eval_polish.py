"""整理质量评估：输入用「模拟 ASR 真实输出」——带标点但断得很碎。

这是真正要整理的形态：ASR 按 VAD 分段，每段都自己加标点，
拼起来就是「一句一断、逗号句号乱飞」。拿这个测才有意义，
用没标点的干净文本测会高估模型能力。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _cfg_guard  # noqa: E402
_cfg_guard.protect()

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine  # noqa: E402

CASES = [
    ("碎句 + 口癖",
     "嗯。我今天上午去了一趟医院。然后医生说。老人恢复得还行。就是需要人照顾。"
     "然后我就想问问。你们这边一天多少钱。"),
    ("一句一断",
     "我母亲刚出院。现在在家里面。需要有人照顾。但是白天我们都要上班。"
     "所以想找个人。白天过来帮忙看着一点。晚上我们自己照顾。"),
    ("长句连逗号",
     "就是这个老人刚做完手术嘛，然后晚上需要有人看着点，我们白天要上班实在顾不过来，"
     "所以想找一个白天能过来的护工，最好是做过医院的，有经验的那种，价格方面怎么算呢。"),
    ("中英混合",
     "嗯我平时用 github 和 vs code 写代码，然后部署用的是 docker，"
     "这些东西你那边能不能也帮我记一下。"),
    ("业务咨询",
     "哎你好，我想问一下你们这个陪护服务，就是老人刚出院在家，白天没人照顾，"
     "能不能派个人过来，一天几个小时，怎么收费，能不能先试两天。"),
]


def main():
    eng = PolishEngine()
    eng.configure(str(CONFIG.get("polish", "server_bin")),
                  str(CONFIG.get("polish", "model_file")), 8080, 0)
    ok, err = eng.ensure()
    if not ok:
        print("模型没起来:", err)
        return 1

    total = 0.0
    for name, text in CASES:
        eng.polish(text, 2)          # 预热前缀，不计入耗时
        t0 = time.time()
        out, e = eng.polish(text, 2)
        dt = time.time() - t0
        total += dt
        print(f"\n--- {name}  {dt:.2f}s ---")
        print("原:", text)
        print("整:", out)
        if e:
            print("错:", e)
    print(f"\n平均 {total / len(CASES):.2f}s")
    eng.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
