"""长句（≥3s）+ 停顿：确认「攒够才断开」没有破坏上一轮的 gap 修复。

对照两组句子长度：
  2.0 秒/句 → 短句，应该合并（不断开），标点交给模型
  4.5 秒/句 → 长句，应该照常断开，gap 必须量得准（1.0 / 2.5 秒）
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dist" / "Verbalkey" / "_internal"))

from src.audio import Recorder  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512
EVAL = ROOT / "logs" / "asr_eval"


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        ch = w.getnchannels()
        d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        d = d.reshape(-1, ch)[:, 0]
    return d.astype(np.float32) / 32768.0


class Fake:
    def __init__(self, vad_cfg):
        self.segs: list[tuple[np.ndarray, bool, int]] = []
        self.rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, vad_cfg,
                            on_segment=self._on,
                            on_error=lambda m: print("  [err]", m))
        self.rec.open = lambda: True

    def _on(self, samples, forced, gap):
        self.segs.append((np.asarray(samples, dtype=np.float32), forced, gap))

    def run(self, x):
        self.rec.start()
        for i in range(0, len(x) - BLK, BLK):
            self.rec._callback(
                np.asarray(x[i:i + BLK], dtype=np.float32).reshape(-1, 1),
                BLK, None, None)
        self.rec.stop()
        t0 = time.time()
        while self.rec._worker and self.rec._worker.is_alive():
            time.sleep(0.05)
            if time.time() - t0 > 10:
                break
        time.sleep(0.1)


def run_case(src, piece, pause, label):
    n = int(piece * SR)
    voice = src[int(1.0 * SR):int(1.0 * SR) + n]
    sil = np.zeros(int(pause * SR), dtype=np.float32)
    x = np.concatenate([voice, sil, voice, sil, voice])
    f = Fake(dict(CONFIG.get("vad") or {}))
    f.run(x)
    print(f"\n{label}：句长 {len(voice)/SR:.1f}s，停顿 {pause}s × 2，"
          f"共 {len(x)/SR:.1f}s → {len(f.segs)} 段")
    for i, (s, forced, gap) in enumerate(f.segs):
        g = "未知" if gap < 0 else f"{gap/SR:.2f}s"
        mark = ""
        if gap >= 0:
            mark = ("  → 句号" if gap / SR >= 1.60
                    else ("  → 逗号" if gap / SR >= 0.70 else "  → 直接接上"))
        print(f"  段{i+1}  {len(s)/SR:5.2f}s  {'硬切' if forced else '自然'}  "
              f"gap={g}{mark}")


def main():
    src = load(EVAL / "fast1.16k.wav")
    for pause in (1.0, 2.5):
        run_case(src, 2.0, pause, f"短句组 停顿{pause}s")
    for pause in (1.0, 2.5):
        run_case(src, 4.5, pause, f"长句组 停顿{pause}s")


if __name__ == "__main__":
    main()
