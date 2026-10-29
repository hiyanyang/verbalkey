"""短句 + 停顿：断开阈值会不会把识别撕碎 —— 用构造素材量化。

真实素材（fast1/raokouling）都是一口气念完，天然没有 0.7~1.6 秒的停顿，
所以拿它测不出差别。这里拿真实语音切成 1.5 秒的小片、片间插 0.9 秒静音
—— 这就是「一句一停地口述」的最小复刻。

三档（同一份音频，只改断开行为）：
  A 每片单独送（等价于修前：gap>=0.70s 就断开，不看长度）
    用 min_asr_samples=1 让新加的门槛失效来复刻
  B 攒够 min_asr_sec(3s) 才允许断开（修后）
  C 永不断开

「C」上下文最全，拿它当参照算 A/B 的 CER —— 差多少就是碎片化的损失。
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
from src.asr import AsrEngine  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402
from src.main import _join_segments  # noqa: E402

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


def cer(ref: str, hyp: str):
    r, h = list(ref), list(hyp)
    n, m = len(r), len(h)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = r[i - 1] == h[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (0 if same else 1))
    return d[n][m] / max(n, 1)


class Fake:
    def __init__(self, vad_cfg, split_sec=None, min_asr_sec=None):
        self.segs: list[tuple[np.ndarray, bool, int]] = []
        self.rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, vad_cfg,
                            on_segment=self._on,
                            on_error=lambda m: print("  [err]", m))
        self.rec.open = lambda: True
        if split_sec is not None:
            self.rec._split_gap_samples = int(split_sec * SR)
        if min_asr_sec is not None:
            self.rec.min_asr_samples = int(min_asr_sec * SR)

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


def build(src: np.ndarray, piece_sec: float, pause_sec: float, n: int) -> np.ndarray:
    """从 src 里均匀取 n 个不重叠小片，片间插静音。"""
    plen = int(piece_sec * SR)
    span = len(src) // n
    out = []
    for k in range(n):
        start = k * span + max(0, (span - plen) // 2)
        out.append(src[start:start + plen])
        if k < n - 1:
            out.append(np.zeros(int(pause_sec * SR), dtype=np.float32))
    return np.concatenate(out)


def main():
    eng = AsrEngine()
    eng.load(MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
             num_threads=6)
    eng.transcribe(load(EVAL / "fast1.16k.wav")[:SR * 5], SR)  # 预热

    cases = [
        ("A 修前(每片单独送)", dict(split_sec=0.70, min_asr_sec=0.001)),
        ("B 修后(攒够3s才断)", dict(split_sec=0.70)),
        ("C 永不断开", dict(split_sec=1e9)),
    ]

    for wav, piece, pause, n in (("fast1.16k.wav", 1.5, 0.9, 8),
                                 ("raokouling.16k.wav", 1.5, 0.9, 10)):
        src = load(EVAL / wav)
        x = build(src, piece, pause, n)
        print(f"\n=== 构造素材：{wav} 切 {n} 片×{piece}s，间插 {pause}s 停顿"
              f" → 共 {len(x)/SR:.1f}s ===")
        res = {}
        for label, kw in cases:
            f = Fake(dict(CONFIG.get("vad") or {}), **kw)
            f.run(x)
            parts = [(eng.transcribe(s, SR), g) for s, _, g in f.segs]
            txt = (_join_segments(parts) if len(parts) > 1
                   else (parts[0][0] if parts else ""))
            res[label] = txt
            lens = "/".join(f"{len(s)/SR:.1f}" for s, _, _ in f.segs)
            print(f"  {label:<20}段数 {len(f.segs):>2}  段长 {lens}")
            print(f"      → {txt[:100]}")
        base = res["C 永不断开"]
        print("  相对「C 永不断开」的 CER（越低越接近最优）：")
        for label in ("A 修前(每片单独送)", "B 修后(攒够3s才断)"):
            print(f"      {label:<20} {cer(base, res[label]) * 100:5.1f}%")


if __name__ == "__main__":
    main()
