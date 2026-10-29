"""断句回归：拿用户真实口述 + 模拟 ASR 分段痕迹，看整理后是否连成完整句子。

用法：D:/vi-venv/Scripts/python.exe tools/probe_break.py
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
from src.main import _join_segments  # noqa: E402
from src.polish import PolishEngine  # noqa: E402

# 每条 = 一轮录音被 VAD 切出的若干段（原话来自 2026-10-28 的 logs/app.log）
SEGMENTS = [
    ("断句抱怨", [
        "另外。这个整体的断句还是有问题。",
        "他不会把我停顿之后的句子连起来。",
        "有的时候我说一个句子。中间会停顿比较久。",
        "但是他整理的时候不会把这个能够表达完整意思的句子连起来。",
        "可能会产生歧义。",
        "而且它默认会优先使用句号。",
        "就更容易引起歧义。",
    ]),
    ("记忆规则", [
        "如果你自己实在判断不了。",
        "是不是该用那种。可能会比较危险的方法。",
        "你可以先问我。",
        "但是不要默认就把这种解决问题的方法给排除。",
        "我们最终的目的是解决问题。",
        "只要在解决问题之后。",
        "把它用在合法合理的。领域就行。",
    ]),
    ("拼音输入法", [
        "另外。我这个是不是也可以把拼音输入法加进去。",
        "然后加上一些热门的词库。",
        "然后根据使用习惯调整词频。",
        "丰富词库。定期更新热门词库。",
        "这个是不是相对来说比较简单。好实现吗。",
    ]),
    ("业务咨询", [
        "哎你好。我想问一下你们这个陪护服务。",
        "就是老人刚出院在家。白天没人照顾。",
        "能不能派个人过来。",
        "一天几个小时。怎么收费。",
        "能不能先试两天。",
    ]),
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
        for tag, segs in SEGMENTS:
            raw = _join_segments(segs)
            out, e = eng.polish(raw, 2)
            print("\n" + "=" * 66)
            print(f"【{tag}】")
            print("拼段后:", raw)
            print("整理后:", out, ("  [" + e + "]") if e else "")
            print(f"句号 {raw.count('。')} → {out.count('。')}，"
                  f"逗号 {raw.count('，')} → {out.count('，')}，"
                  f"字数 {len(raw)} → {len(out)}")
    finally:
        eng.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
