"""验证麦克风采集与 VAD 分句。不需要识别模型。

跑起来后对着麦克风说几句话（中间停顿），脚本打印每次分句的时长。
按 Ctrl+C 结束。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from src.audio import Recorder  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


def main():
    vad = MODELS_DIR / "silero_vad.onnx"
    if not vad.exists():
        print(f"缺少 VAD 模型: {vad}")
        return 1

    count = [0]

    def on_seg(samples: np.ndarray):
        count[0] += 1
        dur = len(samples) / 16000
        peak = float(np.abs(samples).max()) if len(samples) else 0.0
        print(f"  第 {count[0]} 段：{dur:.2f}s  峰值 {peak:.3f}")

    def on_err(msg):
        print("错误:", msg)

    done = []

    rec = Recorder(
        vad_model=str(vad),
        sample_rate=16000,
        vad_cfg=CONFIG.get("vad"),
        device=CONFIG.get("audio", "device"),
        on_segment=on_seg,
        on_error=on_err,
        on_finished=lambda: done.append(True),
    )

    print(f"录音 {SECONDS:.0f} 秒，请说话（说完停一下，让 VAD 断句）…")
    if not rec.start():
        print("麦克风打开失败")
        return 1

    t0 = time.time()
    try:
        while time.time() - t0 < SECONDS:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass

    rec.stop()
    for _ in range(40):
        if done:
            break
        time.sleep(0.1)
    print(f"结束，共 {count[0]} 段，收尾回调 {'已触发' if done else '未触发'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
