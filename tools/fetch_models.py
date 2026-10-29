#!/usr/bin/env python
"""按清单补齐模型文件。

设计原则：仓库里只放「怎么拿」的逻辑，不放「拿走要 3 GB 的东西」。
所有模型都指向上游官方源（ModelScope / GitHub Release），不自己转存网盘。

用法：
    python tools/fetch_models.py --check          只看缺什么，不下载
    python tools/fetch_models.py --group asr      只下识别模型（约 940 MB）
    python tools/fetch_models.py --group polish   只下整理模型（约 940 MB / 1.2 GB）
    python tools/fetch_models.py --group llama    只下 llama-server（约 24 MB）
    python tools/fetch_models.py --all            全下（约 2 GB）

已有的文件会跳过，半截的续传，不重下。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
BIN = ROOT / "bin"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Verbalkey-Setup/1.0"
CHUNK = 1024 * 256
TIMEOUT = 30
RETRY = 3

MS = "https://modelscope.cn/models/{repo}/resolve/master/{path}"
GH = "https://github.com/{repo}/releases/download/{tag}/{file}"

ASR_DIR = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"

# 每个条目：目标路径、参考大小(MB，±3 容差)、多个候选 URL（按顺序试）
# 大小只是用来「一眼判断下全了没」，真正校验用服务端 Content-Length
ASSETS: dict[str, list[dict]] = {
    "vad": [
        {
            "dest": MODELS / "silero_vad.onnx",
            "mb": 0.6,
            "urls": [
                GH.format(repo="k2-fsa/sherpa-onnx", tag="asr-models",
                          file="silero_vad.onnx"),
            ],
        },
    ],
    "asr": [
        {
            "dest": MODELS / ASR_DIR / "conv_frontend.onnx",
            "mb": 42.1,
            "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                               path="model_0.6B/conv_frontend.onnx")],
        },
        {
            "dest": MODELS / ASR_DIR / "encoder.int8.onnx",
            "mb": 174.0,
            "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                               path="model_0.6B/encoder.int8.onnx")],
        },
        {
            "dest": MODELS / ASR_DIR / "decoder.int8.onnx",
            "mb": 720.9,
            "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                               path="model_0.6B/decoder.int8.onnx")],
        },
        {
            "dest": MODELS / ASR_DIR / "tokenizer" / "vocab.json",
            "mb": 2.6,
            "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                               path="tokenizer/vocab.json")],
        },
        {
            "dest": MODELS / ASR_DIR / "tokenizer" / "merges.txt",
            "mb": 1.6,
            "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                               path="tokenizer/merges.txt")],
        },
    ],
    # 整理模型二选一，只下一个就够；两个都下也不冲突
    "polish": [
        {
            "dest": MODELS / "polish" / "MyVoiceTyping-1.5B-Q4_K_M.gguf",
            "mb": 940.4,
            "urls": [MS.format(repo="botaruibo/MyVoiceTyping-1.5b-q4",
                               path="MyVoiceTyping-1.5B-Q4_K_M.gguf")],
            "either": "polish", "variant": "myvoicetyping", "default": True,
            "note": "专为中文语音输入微调，940 MB，效果更对口",
        },
        {
            "dest": MODELS / "polish" / "Qwen3-1.7B.Q4_K_M.gguf",
            "mb": 1223.0,
            "urls": [MS.format(repo="QuantFactory/Qwen3-1.7B-GGUF",
                               path="Qwen3-1.7B.Q4_K_M.gguf")],
            # 上游文件名带点，很多人本地存成横线版，当成同一个东西
            "aliases": [MODELS / "polish" / "Qwen3-1.7B-Q4_K_M.gguf"],
            "either": "polish", "variant": "qwen3",
            "note": "通用 1.7B，1223 MB，不是专为语音输入训练的",
        },
    ],
    "llama": [
        {
            "dest": BIN / "llama" / "llama-b11934-bin-win-cpu-x64.zip",
            "mb": 24.0,
            "urls": [
                GH.format(repo="ggml-org/llama.cpp", tag="b11934",
                          file="llama-b11934-bin-win-cpu-x64.zip"),
                # 上游被墙时的自建镜像（只有 24 MB，可以放 Release）
                GH.format(repo="YorkYanY/verbalkey", tag="llama-b11934",
                          file="llama-b11934-bin-win-cpu-x64.zip"),
            ],
            "unzip_to": BIN / "llama",
            # 判断标准是「能不能跑」，不是「zip 在不在」——解压完的人不需要这个 zip
            "ready_if": BIN / "llama" / "llama-server.exe",
            "note": "llama.cpp CPU 推理程序，解压后约 83 MB",
        },
    ],
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def remote_size(url: str) -> int:
    """拿远端文件总字节数；拿不到返回 -1（不阻断下载，只跳过大小校验）

    ModelScope 的 CDN 不认 HEAD（返回 -1），所以用 Range: bytes=0-0：
    服务端会回 206 + `Content-Range: bytes 0-0/1234567`，总大小在斜杠后面。
    读不到就关掉连接，不会把整个文件拉下来。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr:
                tail = cr.rsplit("/", 1)[1].strip()
                if tail.isdigit():
                    return int(tail)
            return int(r.headers.get("Content-Length") or -1)
    except Exception:
        return -1


def download(url: str, dest: Path) -> bool:
    """断点续传下载。先写 .part，完成才改名，避免半截文件被当成完整。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    total = remote_size(url)

    # 目标文件本身已经完整就跳过，别为了「保险」把 940 MB 重下一遍
    if dest.exists():
        got = size_of(dest)
        if total > 0 and got == total:
            print(f"    已完整（{human(total)}），跳过")
            return True
        if total < 0:
            print("    目标已存在，跳过（拿不到远端大小，无法核对）")
            return True
        print(f"    本地 {human(got)} 与远端 {human(total)} 不符，重下")
        dest.unlink(missing_ok=True)

    have = size_of(part)

    # 没有 Content-Length 又没断点文件 → 老实从头下
    if total > 0 and have >= total:
        part.replace(dest)
        print(f"    已完整（{human(total)}），改用现有文件")
        return True

    headers = {"User-Agent": UA}
    mode = "wb"
    if have > 0 and (total < 0 or have < total):
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
        print(f"    续传：已有 {human(have)}"
              + (f" / {human(total)}" if total > 0 else ""))

    for attempt in range(1, RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if total < 0:
                    total = int(r.headers.get("Content-Length") or -1) + have
                start = time.time()
                done = have
                last = 0.0
                with open(part, mode) as f:
                    while True:
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        now = time.time()
                        if now - last > 0.3:
                            last = now
                            speed = done - have
                            speed = speed / max(now - start, 0.001)
                            if total > 0:
                                pct = done * 100 / total
                                eta = (total - done) / max(speed, 1)
                                sys.stdout.write(
                                    f"\r    {pct:5.1f}%  {human(done)}"
                                    f" / {human(total)}  {human(speed)}/s"
                                    f"  剩 {int(eta)}s   ")
                            else:
                                sys.stdout.write(f"\r    {human(done)}  ")
                            sys.stdout.flush()
                sys.stdout.write("\r" + " " * 78 + "\r")
            got = size_of(part)
            if total > 0 and got < total:
                print(f"    中断：{human(got)} / {human(total)}，将续传")
                headers["Range"] = f"bytes={got}-"
                mode = "ab"
                have = got
                continue
            part.replace(dest)
            print(f"    完成 {human(size_of(dest))}")
            return True
        except urllib.error.HTTPError as e:
            if e.code == 416:      # Range 越界：服务端认为已经下完了
                if part.exists() and total > 0 and size_of(part) >= total:
                    part.replace(dest)
                    print("    完成（服务端确认已到底）")
                    return True
                part.unlink(missing_ok=True)
                print(f"    续传位置失效，重新开始（HTTP {e.code}）")
                headers.pop("Range", None)
                mode = "wb"
                have = 0
            elif e.code in (403, 404):
                print(f"    链接失效（HTTP {e.code}），换下一个源")
                return False
            else:
                print(f"    第 {attempt} 次失败：HTTP {e.code}")
        except Exception as e:
            print(f"    第 {attempt} 次失败：{type(e).__name__} {e}")
        time.sleep(1.5 * attempt)
    return False


def _size_ok(path: Path, a: dict) -> bool:
    if not path.exists():
        return False
    got = size_of(path) / 1048576
    return abs(got - a["mb"]) <= max(3.0, a["mb"] * 0.05)


def status(a: dict) -> str:
    """ok / missing / partial

    判断的是「这个功能能不能跑」，不是「清单里那个文件名在不在」。
    所以 ready_if 优先（llama 解压完就不需要那个 zip），
    其次是 aliases（上游文件名和本地常见改名都算数）。
    """
    if a.get("ready_if") and Path(a["ready_if"]).exists():
        return "ok"
    for cand in [a["dest"], *a.get("aliases", [])]:
        if _size_ok(Path(cand), a):
            return "ok"
    dest = a["dest"]
    part = dest.with_suffix(dest.suffix + ".part")
    return "partial" if part.exists() else "missing"


def check(verbose: bool = True) -> list[dict]:
    missing = []
    for group, items in ASSETS.items():
        if verbose:
            print(f"[{group}]")
        for a in items:
            st = status(a)
            if verbose:
                name = a["dest"].relative_to(ROOT)
                mark = {"ok": "有  ", "partial": "半截", "missing": "缺  "}[st]
                note = f"   {a['note']}" if a.get("note") else ""
                print(f"  {mark} {name}  ({a['mb']:.0f} MB){note}")
            if st != "ok":
                missing.append(a)
    return missing


def fetch(items: list[dict], want: set[str] | None = None) -> int:
    # 组内「二选一」的：已经有一个能用，剩下的就不下，省 1 GB 流量
    ready = {a["either"] for a in items
             if a.get("either") and status(a) == "ok"}

    failed = 0
    for i, a in enumerate(items, 1):
        dest = a["dest"]
        st = status(a)
        rel = dest.relative_to(ROOT)
        if st == "ok":
            hit = a.get("ready_if") or next(
                (p for p in [dest, *a.get("aliases", [])] if Path(p).exists()), dest)
            print(f"[{i}/{len(items)}] 跳过，已有 {Path(hit).relative_to(ROOT)}")
            continue
        if a.get("either") in ready:
            print(f"[{i}/{len(items)}] 跳过 {dest.name}（这组已有一个可用）")
            continue
        v = a.get("variant")
        if v and want and v not in want:
            print(f"[{i}/{len(items)}] 跳过 {dest.name}（按 --variant 指定的来）")
            continue
        if a.get("either") and not a.get("default") and not want:
            print(f"[{i}/{len(items)}] 跳过 {dest.name}"
                  f"（默认只下推荐那个；要它加 --variant {v}）")
            continue
        print(f"[{i}/{len(items)}] {rel}  约 {a['mb']:.0f} MB")
        ok = False
        for url in a["urls"]:
            if download(url, dest):
                ok = True
                break
            if url != a["urls"][-1]:
                print("    换源重试…")
        if not ok:
            failed += 1
            print(f"    失败：所有源都不通。手动下载后放到 {dest}")
            continue
        # zip 类资产顺手解压
        if a.get("unzip_to") and dest.suffix == ".zip":
            target = a["unzip_to"]
            print(f"    解压到 {target.relative_to(ROOT)} …")
            try:
                target.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(dest) as z:
                    z.extractall(target)
                # llama.cpp 的 zip 里带一层同名目录，压平一层
                subs = [p for p in target.iterdir() if p.is_dir()]
                if len(subs) == 1 and any(subs[0].glob("*.exe")):
                    for p in list(subs[0].iterdir()):
                        shutil.move(str(p), str(target / p.name))
                    subs[0].rmdir()
                print("    解压完成")
            except Exception as e:
                print(f"    解压失败（{e}），手动解压 {dest} 到 {target}")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 Verbalkey 的模型文件")
    ap.add_argument("--check", action="store_true", help="只体检，不下载")
    ap.add_argument("--group", help="只处理某几组，逗号分隔：vad,asr,polish,llama")
    ap.add_argument("--all", action="store_true", help="全部下载")
    ap.add_argument("--variant", choices=["myvoicetyping", "qwen3"],
                    help="整理模型选哪个（默认 myvoicetyping）")
    args = ap.parse_args()

    print(f"项目目录：{ROOT}\n")

    if args.check:
        missing = check()
        print()
        if not missing:
            print("全部就绪，可以直接启动。")
        else:
            total = sum(a["mb"] for a in missing)
            print(f"缺 {len(missing)} 个文件，共约 {total:.0f} MB。")
            print("跑 `python tools/fetch_models.py --all` 自动补齐。")
        return 0

    if args.all:
        items = [a for v in ASSETS.values() for a in v]
    elif args.group:
        names = [g.strip() for g in args.group.split(",") if g.strip()]
        bad = [g for g in names if g not in ASSETS]
        if bad:
            print(f"不认识的组：{', '.join(bad)}")
            print(f"可选：{', '.join(ASSETS)}")
            return 2
        items = [a for g in names for a in ASSETS[g]]
    else:
        check()
        print("\n指定 --group 或 --all 才会下载。")
        return 0

    want = {args.variant} if args.variant else None
    failed = fetch(items, want)
    print()
    if failed:
        print(f"{failed} 个文件没拿到。重跑一次可续传。")
        return 1
    print("完成。跑 --check 复查一遍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
