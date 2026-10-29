"""安全打包：PyInstaller 会把 dist/Verbalkey 整个删掉重建，而配置、模型、
语料都在那个目录里。直接敲 pyinstaller --clean 等于把用户数据一起删。

2026-10-29 已经真删过一次：dist/Verbalkey/{config.json,data,logs} 全没了，
data/hotwords/corpus.jsonl（成对语料）跟着丢了。config 能按根目录重建，
语料丢了就真没了 —— 所以现在 data 整目录备份、日志留一份，打完再放回去。

用法：
    D:/vi-venv/Scripts/python.exe tools/build.py

做的事：
    1. 备份 dist/Verbalkey/{config.json, data/, logs/app.log}
    2. 跑 PyInstaller
    3. 重建 models / bin 的目录联接（指向项目根，不重复占 3.1G）
    4. 建回 data / logs / assets 目录，还原数据与配置
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "Verbalkey"
CONFIG = DIST / "config.json"
DATA = DIST / "data"
APP_LOG = DIST / "logs" / "app.log"


def _link(link: Path, target: Path) -> None:
    """把 link 做成指向 target 的目录联接；不行就退回复制。"""
    if link.exists():
        return
    ps = (f"New-Item -ItemType Junction -Path '{link}' "
          f"-Target '{target}' -Force | Out-Null")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps])
    if r.returncode == 0 and link.exists():
        print(f"  联接 {link.name} -> {target}")
        return
    print(f"  联接失败，改为复制 {link.name}（会很慢）")
    shutil.copytree(target, link)


def main() -> int:
    stage = Path(tempfile.mkdtemp(prefix="vk_build_"))
    backup = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else None
    if backup:
        print("已备份 config.json")
    if DATA.exists():
        shutil.copytree(DATA, stage / "data")
        print(f"已备份 data/（{sum(1 for _ in DATA.rglob('*') if _.is_file())} 个文件）")
    if APP_LOG.exists():
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copy2(APP_LOG, stage / "app.log")
        print("已备份 logs/app.log")

    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "verbalkey.spec"],
        cwd=str(ROOT),
    )
    if r.returncode != 0:
        print(f"打包失败。备份还在 {stage}，别删这个目录")
        return r.returncode

    for name in ("models", "bin"):
        _link(DIST / name, ROOT / name)
    for name in ("data", "logs", "assets"):
        (DIST / name).mkdir(parents=True, exist_ok=True)

    if (stage / "data").exists():
        shutil.copytree(stage / "data", DATA, dirs_exist_ok=True)
        print("data/ 已还原")
    if (stage / "app.log").exists():
        shutil.copy2(stage / "app.log", APP_LOG)
        print("logs/app.log 已还原")
    if backup:
        CONFIG.write_text(backup, encoding="utf-8")
        print("config.json 已还原")
    else:
        print("警告：打包前没有 config.json，运行时会用默认配置")
    shutil.rmtree(stage, ignore_errors=True)
    print(f"完成：{DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
