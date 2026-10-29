"""测量麦克风启动延迟：从 start() 到第一块音频到达要多久。

这段时间里用户说的话就是「开头丢字」的来源。
"""
from __future__ import annotations

import time

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 512          # 32 ms


def measure(device=None, rounds=3):
    print(f"{'轮次':>4s} {'start→首块':>12s} {'首块时长':>10s} {'总块数':>7s}")
    for r in range(rounds):
        stamps: list[float] = []

        def cb(indata, frames, t, status):
            stamps.append(time.time())

        t0 = time.time()
        s = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                           blocksize=BLOCK, device=device, callback=cb)
        s.start()
        time.sleep(1.2)
        s.stop()
        s.close()

        if not stamps:
            print(f"{r:>4d} 没有回调")
            continue
        first = stamps[0] - t0
        span = stamps[-1] - stamps[0]
        print(f"{r:>4d} {first * 1000:>10.0f}ms "
              f"{span * 1000:>9.0f}ms {len(stamps):>7d}")


def measure_stop_tail(device=None):
    """stop() 之后还会不会继续收到数据（判断关流是否切掉尾音）。"""
    print("\nstop() 前后的数据块：")
    for r in range(2):
        count = {"before": 0, "after": 0}
        flag = {"stopped": False}

        def cb(indata, frames, t, status):
            if flag["stopped"]:
                count["after"] += 1
            else:
                count["before"] += 1

        s = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                           blocksize=BLOCK, device=device, callback=cb)
        s.start()
        time.sleep(0.8)
        flag["stopped"] = True
        t_stop = time.time()
        s.stop()
        s.close()
        time.sleep(0.3)
        print(f"  第{r + 1}轮 停止前 {count['before']} 块，"
              f"停止后额外收到 {count['after']} 块"
              f"（stop() 本身耗时 {(time.time() - t_stop) * 1000:.0f}ms）")


if __name__ == "__main__":
    print("默认输入设备:", sd.query_devices(kind="input")["name"])
    measure()
    measure_stop_tail()
