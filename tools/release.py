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

安装（四步）
------------
1. 先把下载到的 zip 解压出来
   在压缩包上右键 → 「全部解压缩」→ 选一个位置（桌面、文档、D 盘都行）
2. 进解压出来的 Verbalkey 文件夹，双击 Verbalkey.exe
3. 第一次启动会弹一个窗口，列出要下载的东西（识别模型约 940 MB，
   想连拼音打字一起用就把「拼音输入法」那行也勾上，再 +29 MB），
   点一次「开始下载并安装」，等它下完装完。
   断网了也没关系，重开会接着下，已经下好的一部分不会重来。
4. 托盘出现粉色图标后，按住右 Alt 说话，松开就上屏。

全程只需要你点两次：一次「开始下载并安装」，
一次装拼音时 Windows 弹的管理员授权框（点「是」）。

这三个错别犯
------------
x 在压缩包的预览窗口里直接双击 Verbalkey.exe
  会报「找不到 xxx.dll」或者闪一下就没了 —— 那样文件还没真正落地。
  必须先解压，再进解压出来的文件夹里双击。

x 解压到 C:\\Program Files 或 C:\\Windows 下面
  那两个目录普通权限写不进去，模型会下不下来，装完提示找不到模型。
  解压到桌面、文档、D 盘都行，路径里别带空格和特殊符号最稳。

x 只把 Verbalkey.exe 单独拷到别的地方
  exe 自己只有几 MB，真正的东西在旁边的 _internal 文件夹里，单拷必报错。
  要挪位置、要拷到 U 盘，整个 Verbalkey 文件夹一起挪。

不需要安装 Python，也不需要命令行。
解压后应该是 Verbalkey\\Verbalkey.exe 这一层；如果解压出来是
Verbalkey\\Verbalkey\\Verbalkey.exe，多套了一层，进里层那个就行。

第一次用之前
------------
- 电脑要能上网（只有下载模型这一步需要，之后完全离线）
- 麦克风：设置 → 语音与识别，确认选对了设备
- 拼音输入法：在引导窗口里勾上就会自己下、自己装，只有装引擎那一步
  系统会弹一次管理员授权框，点「是」（Windows 的规矩，绕不过去）。
  当时没勾、以后想补：托盘右键 → 「下载模型与组件…」。
  装完按 Win+空格 切到 Verbalkey 就能打拼音。

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
    # utf-8-sig（带 BOM）：Windows 记事本靠 BOM 判断编码，没有 BOM 的老版本
    # 会按 ANSI 读，整篇中文变乱码。加 3 个字节的 BOM 就能让所有编辑器都认。
    (target / "使用说明.txt").write_text(
        README_TXT.format(ver=APP_VERSION), encoding="utf-8-sig")
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
                   "Verbalkey/_internal/sherpa_onnx",
                   # 拼音字典漏进去是「静默失效」：程序照样跑，只是中文热词
                   # 一条都改不了，日志里才有一行提示。所以必须在发布前挡住。
                   "Verbalkey/_internal/pypinyin/pinyin_dict.json"):
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
