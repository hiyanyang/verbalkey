"""打出「免 Python」的发布包（zip）。

用户侧只要三步：**下载 → 解压 → 双击 Verbalkey.exe**。
不需要装 Python、不需要 pip、不需要命令行 —— PyInstaller 的 onedir 产物
已经把解释器和全部依赖打包进去了，`_internal/` 里那 238 MB 就是它们。

包里**故意不带**这些东西（首次运行会问一次，点确认自己下）：

| 不带 | 原因 |
|---|---|
| models/（938 MB + 可选 940 MB） | 体积；且模型有各自的许可证，不做二次分发 |
| bin/llama（83 MB） | 同上 |
| assets/rime/installer + ice（29 MB） | GPL-3.0 第三方二进制 |
| config.json / data/ / logs/ | 属于别人机器上的隐私数据，绝不能进发布包 |

带的是：exe + `_internal/`（运行时）+ `assets/app.ico` + `assets/rime/deployer`
（librime 官方部署器，BSD-3，4 MB，没有它就没法自动重编译词库）。

用法：
    python tools/release.py            # 用现有 dist 打包
    python tools/release.py --build    # 先跑 tools/build.py 再打包
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "Verbalkey"
OUT_DIR = ROOT / "release"
STAGE = OUT_DIR / "_stage"

# 顶层直接跳过的目录/文件
SKIP_TOP = {"models", "bin", "data", "logs", "config.json"}
# 拼音组件里只留 deployer（BSD），引擎包和词库运行时再下
SKIP_REL = {
    Path("_internal/assets/rime/installer"),
    Path("_internal/assets/rime/ice"),
    Path("_internal/assets/rime/ice.zip"),
}
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache"}

README_TXT = """Verbalkey {ver} —— Windows 本地语音输入
==========================================

怎么用
------
1. 双击 Verbalkey.exe
2. 第一次启动会弹一个窗口，列出要下载的东西（识别模型约 940 MB）
   点「开始下载」，等它下完。断网了也没关系，重开会接着下。
3. 托盘出现粉色图标后，按住右 Alt 说话，松开就上屏。

不需要安装 Python，也不需要命令行。整个文件夹可以拷到 U 盘或别的电脑。
想挪位置就整个文件夹一起挪，别只拷 exe。

第一次用之前
------------
- 电脑要能上网（只有下载模型这一步需要，之后完全离线）
- 麦克风：设置 → 语音与识别，确认选对了设备
- 想连拼音打字一起用：设置 → 拼音输入法 → 安装拼音输入法（会弹一次授权框）

数据放在哪
----------
config.json    设置
logs/          日志（含每次上屏的文本，介意就删）
data/          从你的改正里学到的热词语料（明文）
models/        识别与整理模型
都是纯本地文件，删掉不影响程序启动。

卸载
----
设置里关掉「开机自启」，退出程序，删掉这个文件夹即可。
（可选拼音装了的话，到「设置 → 应用」里卸掉 Weasel 引擎）
"""


def _copy(src: Path, dst: Path) -> int:
    """按排除规则复制。返回复制的文件数。"""
    count = 0
    for base, dirs, files in os.walk(src):
        base_path = Path(base)
        rel = base_path.relative_to(src)
        # 顶层排除
        if rel == Path("."):
            dirs[:] = [d for d in dirs if d not in SKIP_TOP]
        # 通用垃圾目录
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        # 拼音原始组件（安装包/词库）不打进包
        dirs[:] = [d for d in dirs if (rel / d) not in SKIP_REL]
        for f in files:
            if (rel / f) in SKIP_REL:
                continue
            if rel == Path(".") and f in SKIP_TOP:
                continue
            s = base_path / f
            d = dst / rel / f
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            count += 1
    return count


def _zip_dir(folder: Path, out: Path) -> None:
    tmp = out.with_suffix(".zip.part")
    tmp.unlink(missing_ok=True)
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _dirs, files in os.walk(folder):
            for f in files:
                p = Path(base) / f
                z.write(p, p.relative_to(folder.parent))
                n += 1
    tmp.replace(out)
    print(f"  压缩 {n} 个文件")


def main() -> int:
    ap = argparse.ArgumentParser(description="打发布包")
    ap.add_argument("--build", action="store_true", help="先跑 tools/build.py")
    ap.add_argument("--no-zip", action="store_true", help="只铺目录，不压缩")
    args = ap.parse_args()

    if args.build:
        print("== 1/4 打包 exe ==")
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "build.py")],
                           cwd=str(ROOT))
        if r.returncode != 0:
            print("打包失败，停止")
            return r.returncode

    if not (DIST / "Verbalkey.exe").exists():
        print(f"找不到 {DIST / 'Verbalkey.exe'}，先跑 tools/build.py")
        return 1

    sys.path.insert(0, str(ROOT))
    from src.config import APP_VERSION  # noqa: E402

    print("== 2/4 铺到暂存目录 ==")
    shutil.rmtree(STAGE, ignore_errors=True)
    target = STAGE / "Verbalkey"
    n = _copy(DIST, target)
    (target / "使用说明.txt").write_text(
        README_TXT.format(ver=APP_VERSION), encoding="utf-8")
    print(f"  {n} 个文件")

    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    print(f"  解压后 {size / 1048576:.0f} MB")

    if args.no_zip:
        print(f"完成（未压缩）：{target}")
        return 0

    print("== 3/4 压缩 ==")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"Verbalkey-{APP_VERSION}-win64.zip"
    t0 = time.time()
    _zip_dir(target, out)
    print(f"  {out.name}  {out.stat().st_size / 1048576:.0f} MB"
          f"（{time.time() - t0:.0f}s）")

    print("== 4/4 复查 ==")
    bad = []
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    for needle in ("Verbalkey/Verbalkey.exe", "Verbalkey/使用说明.txt",
                   "Verbalkey/_internal/PySide6/pyside6.abi3.dll",
                   "Verbalkey/_internal/sherpa_onnx"):
        if not any(x.startswith(needle) for x in names):
            bad.append(f"缺 {needle}")
    for forbidden in ("Verbalkey/models/", "Verbalkey/logs/", "Verbalkey/data/",
                      "Verbalkey/config.json",
                      "Verbalkey/_internal/assets/rime/ice/",
                      "Verbalkey/_internal/assets/rime/installer/"):
        hit = [x for x in names if x.startswith(forbidden)]
        if hit:
            bad.append(f"混进了 {forbidden}（{len(hit)} 个文件）")
    if bad:
        print("  发布包有问题：")
        for b in bad:
            print("   -", b)
        return 1
    print("  检查通过：运行时齐全、模型/日志/隐私文件都没混进去")
    print(f"\n成品：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
