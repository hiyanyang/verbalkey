"""模型与第三方组件的下载器。

四处调用它：
- 首次运行引导窗口 `src/ui/firstrun.py` —— 用户不装任何东西，点一次确认就下完
- 设置页（补装整理模型、补装拼音组件）
- 命令行 `tools/fetch_models.py --check / --group`
- 打包脚本（校验发布包不含模型）

设计约束（每条都是踩出来的）：

1. **清单只放「怎么拿」**，不放文件本身。模型合计 3 GB，不可能进仓库。
2. **只指上游官方源**，不做二次分发。网盘链接会烂（见过项目 README 里的分享链接
   过期，作者自己都没权限了）。
3. **断点续传必须真做**。国内断一次很常见，要从头下用户就放弃了。
4. **ModelScope 的 CDN 不认 HEAD**（返回 -1），所以用 `Range: bytes=0-0`
   读 `Content-Range: bytes 0-0/总长` 拿真实大小。
5. **判断「能不能跑」，而不是「文件名在不在」**：llama 解压完就不需要那个 zip；
   上游文件名有点/横线两种写法，都得算数。
"""

from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .config import APP_DIR, MODELS_DIR

BIN_DIR = APP_DIR / "bin"


def rime_dir() -> Path:
    """拼音组件放哪 —— 必须和 rime_setup._bundle_root() 的判断顺序一致。

    打包后 PyInstaller 把随包资源塞进 `_internal/`，部署器就在
    `_internal/assets/rime/deployer`。下载的引擎包和词库必须落在同一层，
    否则 rime_setup.bundled_ice() 找不到，用户点了「安装」还是报缺词库。
    """
    for p in (APP_DIR / "_internal" / "assets" / "rime",
              APP_DIR / "assets" / "rime"):
        if p.is_dir():
            return p
    return APP_DIR / "assets" / "rime"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Verbalkey-Setup/1.0"
CHUNK = 1024 * 256
TIMEOUT = 30
RETRY = 3

MS = "https://modelscope.cn/models/{repo}/resolve/master/{path}"
GH = "https://github.com/{repo}/releases/download/{tag}/{file}"
# GitHub 在国内经常只有几十 KB/s 甚至连不上，走一层镜像重试
GHP = "https://gh-proxy.com/https://github.com/{repo}/releases/download/{tag}/{file}"

ASR_DIR = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"

WEASEL_VER = "0.17.4"
WEASEL_FILE = f"weasel-{WEASEL_VER}.0-installer.exe"

# 进度回调：progress(已下字节, 总字节[未知为 -1], 速度 字节/秒)
Progress = Callable[[int, int, float], None]
# 明细回调：log("正在下载识别模型…")
Log = Callable[[str], None]
# 取消回调：返回 True 就中断（会保留 .part，下次续传）
Stop = Callable[[], bool]


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


def groups() -> dict[str, list[dict]]:
    """按用途分组。每次调用重新拼路径，避免 APP_DIR 变化后指向旧目录。"""
    return {
        "vad": [
            {
                "dest": MODELS_DIR / "silero_vad.onnx",
                "mb": 0.6,
                "label": "语音活动检测",
                "urls": [
                    GH.format(repo="k2-fsa/sherpa-onnx", tag="asr-models",
                              file="silero_vad.onnx"),
                    GHP.format(repo="k2-fsa/sherpa-onnx", tag="asr-models",
                               file="silero_vad.onnx"),
                ],
            },
        ],
        "asr": [
            {
                "dest": MODELS_DIR / ASR_DIR / "conv_frontend.onnx",
                "mb": 42.1,
                "label": "识别模型 1/5",
                "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                                   path="model_0.6B/conv_frontend.onnx")],
            },
            {
                "dest": MODELS_DIR / ASR_DIR / "encoder.int8.onnx",
                "mb": 174.0,
                "label": "识别模型 2/5",
                "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                                   path="model_0.6B/encoder.int8.onnx")],
            },
            {
                "dest": MODELS_DIR / ASR_DIR / "decoder.int8.onnx",
                "mb": 720.9,
                "label": "识别模型 3/5（最大，最慢）",
                "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                                   path="model_0.6B/decoder.int8.onnx")],
            },
            {
                "dest": MODELS_DIR / ASR_DIR / "tokenizer" / "vocab.json",
                "mb": 2.6,
                "label": "识别模型 4/5",
                "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                                   path="tokenizer/vocab.json")],
            },
            {
                "dest": MODELS_DIR / ASR_DIR / "tokenizer" / "merges.txt",
                "mb": 1.6,
                "label": "识别模型 5/5",
                "urls": [MS.format(repo="zengshuishui/Qwen3-ASR-onnx",
                                   path="tokenizer/merges.txt")],
            },
        ],
        # 整理模型二选一，只下一个就够
        "polish": [
            {
                "dest": MODELS_DIR / "polish" / "MyVoiceTyping-1.5B-Q4_K_M.gguf",
                "mb": 940.4,
                "label": "书面化模型（推荐）",
                "urls": [MS.format(repo="botaruibo/MyVoiceTyping-1.5b-q4",
                                   path="MyVoiceTyping-1.5B-Q4_K_M.gguf")],
                "either": "polish", "variant": "myvoicetyping", "default": True,
                "note": "专为中文语音输入微调",
            },
            {
                "dest": MODELS_DIR / "polish" / "Qwen3-1.7B.Q4_K_M.gguf",
                "mb": 1223.0,
                "label": "书面化模型（通用 1.7B）",
                "urls": [MS.format(repo="QuantFactory/Qwen3-1.7B-GGUF",
                                   path="Qwen3-1.7B.Q4_K_M.gguf")],
                # 上游文件名带点，很多人本地存成横线版，当成同一个东西
                "aliases": [MODELS_DIR / "polish" / "Qwen3-1.7B-Q4_K_M.gguf"],
                "either": "polish", "variant": "qwen3",
                "note": "通用模型，不是专为语音输入训练的",
            },
        ],
        "llama": [
            {
                "dest": BIN_DIR / "llama" / "llama-b11934-bin-win-cpu-x64.zip",
                "mb": 24.0,
                "label": "整理引擎 llama.cpp",
                "urls": [
                    GH.format(repo="ggml-org/llama.cpp", tag="b11934",
                              file="llama-b11934-bin-win-cpu-x64.zip"),
                    GHP.format(repo="ggml-org/llama.cpp", tag="b11934",
                               file="llama-b11934-bin-win-cpu-x64.zip"),
                ],
                "unzip_to": BIN_DIR / "llama",
                # 判断标准是「能不能跑」，不是「zip 在不在」
                "ready_if": BIN_DIR / "llama" / "llama-server.exe",
            },
        ],
        # 拼音输入是可选项：引擎必须装进系统（TSF 要求），词库是纯数据。
        # 都是第三方 GPL-3.0 组件，所以不打进发布包，运行时按需下。
        "pinyin": [
            {
                "dest": rime_dir() / "installer" / WEASEL_FILE,
                "mb": 11.9,
                "label": "拼音引擎（小狼毫）",
                "urls": [
                    GH.format(repo="rime/weasel", tag=WEASEL_VER,
                              file=WEASEL_FILE),
                    GHP.format(repo="rime/weasel", tag=WEASEL_VER,
                               file=WEASEL_FILE),
                    GHP.format(repo="rime/weasel", tag=WEASEL_VER,
                               file=WEASEL_FILE).replace(
                                   "https://gh-proxy.com/", "https://ghfast.top/"),
                ],
                "note": "装的时候会弹一次系统授权框，点「是」",
            },
            {
                "dest": rime_dir() / "ice.zip",
                "mb": 16.9,
                "label": "拼音词库（雾凇拼音）",
                "urls": [
                    "https://gh-proxy.com/https://github.com/iDvel/rime-ice/"
                    "archive/refs/heads/main.zip",
                    "https://ghfast.top/https://github.com/iDvel/rime-ice/"
                    "archive/refs/heads/main.zip",
                    "https://codeload.github.com/iDvel/rime-ice/zip/refs/heads/main",
                ],
                "unzip_to": rime_dir() / "ice",
                "strip_top": True,
                # 解压出来的东西才是真的，zip 可以删
                "ready_if": rime_dir() / "ice" / "rime_ice.schema.yaml",
                "cleanup_zip": True,
            },
        ],
    }


def remote_size(url: str) -> int:
    """拿远端文件总字节数；拿不到返回 -1（不阻断下载，只跳过大小校验）。"""
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


def download(url: str, dest: Path, progress: Progress | None = None,
             stop: Stop | None = None) -> bool:
    """断点续传下载。先写 .part，完成才改名，避免半截文件被当成完整。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    total = remote_size(url)

    if dest.exists():
        got = size_of(dest)
        if total > 0 and got == total:
            if progress:
                progress(got, total, 0.0)
            return True
        if total < 0:
            return True
        dest.unlink(missing_ok=True)

    have = size_of(part)
    if total > 0 and have >= total:
        part.replace(dest)
        return True

    headers = {"User-Agent": UA}
    mode = "wb"
    if have > 0 and (total < 0 or have < total):
        headers["Range"] = f"bytes={have}-"
        mode = "ab"

    for attempt in range(1, RETRY + 1):
        if stop and stop():
            return False
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
                        if stop and stop():
                            # 保留 .part，下次接着下
                            return False
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        now = time.time()
                        if progress and now - last > 0.2:
                            last = now
                            speed = (done - have) / max(now - start, 0.001)
                            progress(done, total, speed)
            got = size_of(part)
            if total > 0 and got < total:
                headers["Range"] = f"bytes={got}-"
                mode = "ab"
                have = got
                continue
            part.replace(dest)
            if progress:
                progress(size_of(dest), size_of(dest), 0.0)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 416:      # Range 越界：服务端认为已经下完了
                if part.exists() and total > 0 and size_of(part) >= total:
                    part.replace(dest)
                    return True
                part.unlink(missing_ok=True)
                headers.pop("Range", None)
                mode = "wb"
                have = 0
            elif e.code in (403, 404):
                return False
            else:
                pass
        except Exception:
            pass
        time.sleep(1.5 * attempt)
    return False


def _size_ok(path: Path, a: dict) -> bool:
    if not path.exists():
        return False
    got = size_of(path) / 1048576
    # 词库这种「解压出来的目录」没有准确体积，只要求存在
    if a.get("ready_if"):
        return True
    return abs(got - a["mb"]) <= max(3.0, a["mb"] * 0.05)


def status(a: dict) -> str:
    """ok / partial / missing。判断「能不能跑」，不是「文件名在不在」。"""
    ready = a.get("ready_if")
    if ready and Path(ready).exists():
        return "ok"
    for cand in [a["dest"], *a.get("aliases", [])]:
        if _size_ok(Path(cand), a):
            return "ok"
    dest = Path(a["dest"])
    part = dest.with_suffix(dest.suffix + ".part")
    return "partial" if part.exists() else "missing"


def _unzip(zip_path: Path, target: Path, strip_top: bool) -> bool:
    r"""解压。strip_top=True 压平顶层目录（rime-ice 的 zip 顶上是 rime-ice-main/）。

    顺手跳过仓库里对本项目没用的目录：others/ 是别人的方案和插件，
    铺进 %APPDATA%\Rime 会和我们的配置打架；.github/build 更没必要。
    """
    SKIP = ("others/", ".github/", "build/", ".git/")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        if strip_top:
            for info in z.infolist():
                name = info.filename
                if "/" not in name or info.is_dir():
                    continue
                rel = name.split("/", 1)[1]
                if not rel or rel.startswith(SKIP):
                    continue
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            return True
        z.extractall(target)
        subs = [p for p in target.iterdir() if p.is_dir()]
        if len(subs) == 1 and any(subs[0].glob("*.exe")):
            for p in list(subs[0].iterdir()):
                shutil.move(str(p), str(target / p.name))
            subs[0].rmdir()
        return True


def fetch(items: list[dict], want: set[str] | None = None,
          on_log: Log | None = None,
          on_progress: Progress | None = None,
          stop: Stop | None = None,
          on_item: Callable[[int, int, dict], None] | None = None) -> list[str]:
    """按清单补齐。返回失败条目的 label 列表（空 = 全部就绪）。"""
    def log(msg: str):
        if on_log:
            on_log(msg)

    ready = {a["either"] for a in items
             if a.get("either") and status(a) == "ok"}
    failed: list[str] = []

    for i, a in enumerate(items, 1):
        if stop and stop():
            log("已取消")
            break
        label = a.get("label") or Path(a["dest"]).name
        if status(a) == "ok":
            log(f"跳过 {label}（已有）")
            continue
        if a.get("either") in ready:
            log(f"跳过 {label}（这组已有一个可用）")
            continue
        v = a.get("variant")
        if v and want and v not in want:
            continue
        if a.get("either") and not a.get("default") and not want:
            log(f"跳过 {label}（默认只下推荐的；要它加 --variant {v}）")
            continue

        if on_item:
            on_item(i, len(items), a)
        log(f"下载 {label}（约 {a['mb']:.0f} MB）")

        ok = False
        for url in a["urls"]:
            if download(url, a["dest"], on_progress, stop):
                ok = True
                break
            if stop and stop():
                break
            log(f"  这个源不通，换下一个")
        if not ok:
            if stop and stop():
                log("已取消")
                break
            failed.append(label)
            log(f"  失败：所有源都不通")
            continue

        unzip_to = a.get("unzip_to")
        if unzip_to and str(a["dest"]).endswith(".zip"):
            log(f"解压 {label}…")
            try:
                _unzip(Path(a["dest"]), Path(unzip_to), bool(a.get("strip_top")))
                if a.get("cleanup_zip"):
                    Path(a["dest"]).unlink(missing_ok=True)
                log("  解压完成")
            except Exception as e:
                failed.append(label)
                log(f"  解压失败：{e}")
    return failed


def check(group_names: list[str] | None = None,
          verbose: bool = False) -> list[dict]:
    """返回缺失条目。verbose=True 时逐条打印（命令行用）。"""
    g = groups()
    names = group_names or list(g)
    missing: list[dict] = []
    for name in names:
        if verbose:
            print(f"[{name}]")
        for a in g.get(name, []):
            st = status(a)
            # 展示「真正起作用的那份」：llama 解压完展示 exe，词库解压完展示目录，
            # 否则清单里写着 24 MB 的 zip 明明已经删了却显示「有」，看着像 bug
            ready = a.get("ready_if")
            shown = Path(ready) if ready and Path(ready).exists() else Path(a["dest"])
            if verbose:
                mark = {"ok": "有  ", "partial": "半截", "missing": "缺  "}[st]
                try:
                    rel = shown.relative_to(APP_DIR)
                except ValueError:
                    rel = shown
                note = f"   {a['note']}" if a.get("note") else ""
                print(f"  {mark} {rel}  ({a['mb']:.0f} MB){note}")
            if st != "ok":
                missing.append(a)
    return missing


def estimate_mb(items: list[dict]) -> float:
    return sum(a["mb"] for a in items)


def select(names: list[str], want: set[str] | None = None) -> list[dict]:
    """按组名取清单，并丢掉「可选变体里不选的那个」。"""
    g = groups()
    items: list[dict] = []
    for n in names:
        for a in g.get(n, []):
            v = a.get("variant")
            if v and want and v not in want:
                continue
            if a.get("either") and not a.get("default") and not want:
                continue
            items.append(a)
    return items
