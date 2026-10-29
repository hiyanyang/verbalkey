"""A/B 对照：整段重解用「削过静音的拼接」还是「原始录音」。

同一段音频走一遍真实 Recorder，然后分别拿两种口径喂 ASR：
  旧 = 把送出去识别的分段（已削首尾）拼起来  ← 改动前的 _utterance
  新 = 原始音频（未削）                      ← 改动后
输出：两边尾巴还剩多少静音、末尾 12 个字识别成什么。

    python tools/probe_tail4.py
    python tools/probe_tail4.py --asr     # 连识别一起跑（慢，每段约 3~6 秒）
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

from src.audio import Recorder, _trim_silence  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        assert w.getframerate() == SR
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def tail_silence_ms(x: np.ndarray, thr: float = 0.004) -> float:
    """末尾连续多长的 20ms 帧都低于能量阈值。"""
    frame = int(SR * 0.02)
    n = len(x) // frame
    if n < 1:
        return 0.0
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    cnt = 0
    for v in rms[::-1]:
        if v > thr:
            break
        cnt += 1
    return cnt * 20.0


class Fake:
    def __init__(self, vad_cfg):
        self.segs: list[np.ndarray] = []
        self.rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, vad_cfg,
                            on_segment=self._on_seg,
                            on_error=lambda m: print("  [err]", m))
        self.rec.open = lambda: True

    def _on_seg(self, samples, forced, gap):
        self.segs.append(np.asarray(samples, dtype=np.float32))

    def feed(self, x: np.ndarray):
        for i in range(0, len(x) - BLK, BLK):
            self.rec._callback(x[i:i + BLK].reshape(-1, 1), BLK, None, None)


def run(name: str, audio: np.ndarray, tail_ms: int, do_asr: bool):
    f = Fake(dict(CONFIG.get("vad") or {}))
    f.rec.start()
    f.feed(audio)
    if tail_ms:
        f.feed(np.zeros(int(SR * tail_ms / 1000), dtype=np.float32))
    t0 = time.time()
    f.rec.stop()
    while f.rec._worker and f.rec._worker.is_alive():
        time.sleep(0.05)
        if time.time() - t0 > 8:
            break
    time.sleep(0.1)

    new = f.rec.take_utterance()
    old = (np.concatenate(f.segs) if len(f.segs) > 1
           else (f.segs[0] if f.segs else np.zeros(0, dtype=np.float32)))

    print(f"\n=== {name}  原音频 {len(audio)/SR:.2f}s  "
          f"松手后静音 {tail_ms}ms ===")
    print(f"分段 {len(f.segs)} 段: {[f'{len(s)/SR:.2f}s' for s in f.segs]}")
    for label, x in (("旧(削过拼接)", old), ("新(原始)   ", new)):
        if x is None or not len(x):
            print(f"  {label} 空")
            continue
        print(f"  {label} 总长 {len(x)/SR:5.2f}s  "
              f"尾静音 {tail_silence_ms(x):4.0f}ms  头静音 {tail_silence_ms(x[::-1]):4.0f}ms")

    if not do_asr:
        return
    from src.asr import AsrEngine
    eng = AsrEngine()
    eng.load(MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
             num_threads=4)
    for label, x in (("旧(削过拼接)", old), ("新(原始)   ", new)):
        if x is None or not len(x):
            continue
        t = time.time()
        txt = eng.transcribe(x, SR)
        print(f"  {label} {time.time()-t:5.2f}s  "
              f"{len(txt):3d}字  末12字「{txt[-12:]}」")


def main():
    do_asr = "--asr" in sys.argv
    cfg = dict(CONFIG.get("vad") or {})
    print("vad 配置:", {k: cfg.get(k) for k in
                       ("threshold", "silence_ms", "min_speech_ms",
                        "max_segment_s", "max_latency_s", "min_asr_sec")})
    files = sorted((ROOT / "logs" / "asr_eval").glob("*.wav"))
    if not files:
        files = list((MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
                      / "test_wavs").glob("*.wav"))[:1]
    for p in files:
        x = load(p)
        for tail in (0, 500):
            run(p.stem, x, tail, do_asr)


if __name__ == "__main__":
    main()
