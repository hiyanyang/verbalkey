"""打包入口。

单独写一个 entry 而不是直接打 src/main.py：PyInstaller 会把入口脚本
当成 __main__ 执行，而 src/main.py 里的绝对导入又会把 src 包导入一次，
容易出现同名模块两份实例（截图里表现为设置改了不生效）。

另外给打包版加一个崩溃日志：windowed 模式没有控制台，异常一出现
进程就静默消失，用户只会看到「双击没反应」。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _install_crash_log() -> None:
    def hook(exc_type, exc, tb):
        try:
            base = Path(sys.executable).resolve().parent
        except Exception:
            base = Path.cwd()
        try:
            d = base / "logs"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "crash.log", "a", encoding="utf-8") as f:
                f.write("=" * 64 + "\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = hook


_install_crash_log()

# 源码运行时项目根要进 sys.path；打包后模块都在内存里，这句无害
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.main import main  # noqa: E402

if __name__ == "__main__":
    main()
