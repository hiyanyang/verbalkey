"""把 C++ 层的诊断输出收进日志。

为什么需要
----------
模型解码出问题时，sherpa-onnx 自己会打印非常明确的诊断，例如：

    qwen3-asr: decode collapsed into a repetition loop after 64 tokens
    qwen3-asr: context_len (1162) exceeds max_total_len (1024)

这些是 C++ 直接 write 到 fd 2 的，Python 的 logging 一条都抓不到 ——
2026-10-30 那次「识别出复读机」和 10-31 那次「草稿里冒出人权观察」，
日志里都只有我们自己记的字数，只能靠反推。有了这个通道，下次出事
可以先看现成的诊断，不用猜。

为什么用文件而不是管道
----------------------
管道是典型做法，但有个致命副作用：读线程一旦挂掉，管道缓冲区（64KB）
写满后，C++ 那边的 write 会**永久阻塞** —— 表现是整个程序卡住、不报错、
不写日志，比原来的问题严重得多。写文件没有这个风险，写不进去也只是
少几行日志。

代价：诊断是异步转进 app.log 的（每 0.4 秒扫一次），时间戳比真实时刻
晚 0~0.4 秒，行首带 `[native]` 前缀。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 单个文件上限。超过就在下次启动时换一个，只留上一份 ——
# 和 app.log 同一个思路：够查最近几次，不把硬盘吃满。
MAX_BYTES = 2 * 1024 * 1024
_POLL_SEC = 0.4

_state: dict = {"fd": None, "path": None, "stop": None, "thread": None}


def install(logs_dir) -> str:
    """把 fd 2 重定向到 logs/native.log，并起线程转发进主日志。

    返回日志文件路径；任何一步失败都返回空串并保持原样（不影响主流程）。
    """
    if _state["thread"] is not None:
        return str(_state["path"] or "")

    path = Path(logs_dir) / "native.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            old = path.with_name("native.log.1")
            if old.exists():
                old.unlink()
            path.replace(old)
        f = open(path, "ab", buffering=0)
    except Exception as e:                      # noqa: BLE001
        log.warning("无法创建 native.log，跳过 C++ 诊断捕获: %s", e)
        return ""

    try:
        os.dup2(f.fileno(), 2)
    except Exception as e:                      # noqa: BLE001
        log.warning("无法重定向 stderr，跳过 C++ 诊断捕获: %s", e)
        f.close()
        return ""

    # 起点设在重定向之后的文件末尾：只转发这一次运行新产生的行，
    # 不把上一次运行留下的内容再灌一遍。
    try:
        start = path.stat().st_size
    except OSError:
        start = 0

    stop = threading.Event()
    th = threading.Thread(target=_tail, args=(path, start, stop),
                          daemon=True, name="native-log")
    th.start()
    _state.update(fd=f, path=path, stop=stop, thread=th)
    log.info("C++ 诊断输出已接管，写入 %s", path.name)
    return str(path)


def _tail(path: Path, pos: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            size = path.stat().st_size
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(size - pos)
                pos = size
                # 只处理完整的行，半行留到下一轮
                end = data.rfind(b"\n")
                if end < 0:
                    pos -= len(data)
                else:
                    pos -= len(data) - (end + 1)
                    text = data[:end].decode("utf-8", "replace")
                    for line in text.splitlines():
                        line = line.strip()
                        if line:
                            log.warning("[native] %s", line[:600])
        except Exception:                       # noqa: BLE001
            pass                                # 读不到就下一轮再试，绝不阻塞
        stop.wait(_POLL_SEC)


def shutdown() -> None:
    """退出前调用：停掉转发线程（守护线程，不调用也会随进程结束）。"""
    stop = _state.get("stop")
    if stop is not None:
        stop.set()
