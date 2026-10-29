#!/usr/bin/env python
"""补齐 Verbalkey 的模型与第三方组件（命令行入口）。

真正的逻辑在 `src/fetch.py`，因为 exe 里的首次运行引导窗口也要用它。
这里只是把同一份清单接到命令行上，方便开发和在没装 GUI 的机器上预先下好。

用法：
    python tools/fetch_models.py --check            只看缺什么，不下载
    python tools/fetch_models.py --group asr        只下识别模型（约 940 MB）
    python tools/fetch_models.py --group polish     只下整理模型（约 940 MB）
    python tools/fetch_models.py --group pinyin     只下拼音组件（约 42 MB）
    python tools/fetch_models.py --all              全下

已有的跳过，半截的续传，不重下。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许从仓库根直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fetch as F  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 Verbalkey 的模型文件")
    ap.add_argument("--check", action="store_true", help="只体检，不下载")
    ap.add_argument("--group", help="只处理某几组，逗号分隔："
                                   "vad,asr,polish,llama,pinyin")
    ap.add_argument("--all", action="store_true", help="全部下载")
    ap.add_argument("--variant", choices=["myvoicetyping", "qwen3"],
                    help="整理模型选哪个（默认 myvoicetyping）")
    args = ap.parse_args()

    print(f"程序目录：{F.APP_DIR}\n")

    if args.check or not (args.all or args.group):
        names = None
        if args.group:
            names = [g.strip() for g in args.group.split(",") if g.strip()]
            bad = [g for g in names if g not in F.groups()]
            if bad:
                print(f"不认识的组：{', '.join(bad)}")
                print(f"可选：{', '.join(F.groups())}")
                return 2
        missing = F.check(names, verbose=True)
        print()
        if not missing:
            print("全部就绪，可以直接启动。")
            return 0
        print(f"缺 {len(missing)} 个文件，共约 {F.estimate_mb(missing):.0f} MB。")
        print("加 --all 自动补齐。")
        return 0

    if args.all:
        names = list(F.groups())
    else:
        names = [g.strip() for g in args.group.split(",") if g.strip()]
        bad = [g for g in names if g not in F.groups()]
        if bad:
            print(f"不认识的组：{', '.join(bad)}")
            print(f"可选：{', '.join(F.groups())}")
            return 2

    want = {args.variant} if args.variant else None
    items = F.select(names, want)

    state = {"total": 0, "label": ""}

    def on_item(i: int, n: int, a: dict):
        state["label"] = a.get("label") or Path(a["dest"]).name
        print(f"[{i}/{n}] {state['label']}  约 {a['mb']:.0f} MB")

    def on_log(msg: str):
        print(f"    {msg}")

    def on_progress(done: int, total: int, speed: float):
        if total > 0:
            pct = done * 100 / total
            eta = (total - done) / max(speed, 1) if speed else 0
            line = (f"\r    {pct:5.1f}%  {F.human(done)} / {F.human(total)}"
                    f"  {F.human(speed)}/s  剩 {int(eta)}s   ")
        else:
            line = f"\r    {F.human(done)}  {F.human(speed)}/s   "
        sys.stdout.write(line)
        sys.stdout.flush()

    failed = F.fetch(items, want, on_log=on_log, on_progress=on_progress,
                     on_item=on_item)
    sys.stdout.write("\r" + " " * 78 + "\r")
    if failed:
        print(f"{len(failed)} 项没拿到：{'、'.join(failed)}")
        print("重跑一次会续传。")
        return 1
    print("完成。跑 --check 复查一遍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
