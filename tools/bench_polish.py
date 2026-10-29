"""整理耗时实测：冷启动 / 热运行 / 两档强度 / 预热收益。

自己拉起 llama-server，跑完关掉，不影响正在运行的应用。
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine  # noqa: E402

TEXTS = {
    "短 22 字": "嗯那个我想问一下咱们这个陪护服务一天多少钱",
    "中 46 字": "嗯那个我想问一下咱们这个陪护服务一天是管多少钱然后是不是可以按小时算我母亲刚出院",
    "长 88 字": ("嗯那个我想问一下咱们这个陪护服务一天是管多少钱然后是不是可以按小时算"
                 "我母亲刚出院嘛现在在家里面需要有人照顾但是白天我们都要上班所以想找个人"
                 "白天过来帮忙看着一点"),
}


def main() -> int:
    eng = PolishEngine()
    eng.configure(
        str(CONFIG.get("polish", "server_bin")),
        str(CONFIG.get("polish", "model_file")),
        int(CONFIG.get("polish", "port")),
        0,      # 不自动卸载，测完手动 kill
    )

    print("冷启动计时…")
    t0 = time.time()
    ok, err = eng.ensure()
    cold = time.time() - t0
    if not ok:
        print("启动失败:", err)
        return 1
    print(f"  冷启动 {cold:.2f}s（这段在「按下热键」时就已经跑掉了）")

    for level in (1, 2):
        print(f"\n--- 强度 L{level} ---")
        for name, text in TEXTS.items():
            eng.polish(text, level)                       # 预热前缀缓存
            times = []
            for _ in range(3):
                t = time.time()
                out, e = eng.polish(text, level)
                times.append(time.time() - t)
            med = statistics.median(times)
            print(f"  {name}: {med:.2f}s   输出 {len(out)} 字")
            if level == 2 and name == "中 46 字":
                print(f"    原文: {text}")
                print(f"    整理: {out}")

    eng.kill()
    print("\n已关闭测试用的 llama-server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
