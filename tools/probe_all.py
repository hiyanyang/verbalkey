"""验证全程缓冲能把 VAD 丢弃的字救回来。

场景：一段语音 + 700ms 停顿 + 一个孤立的短「字」 + 700ms 停顿 + 一段语音。
VAD 的 min_speech_ms=400，夹在两段长停顿中间的短字会被判成噪声丢掉
（silero 官方语义：「短于此值的语音段丢弃」）。真实病历：
「识别锁被[占]十秒以上」两次独立识别都少这个字。

对比两条口径：
  旧（改动前）= VAD 分段拼起来  → 丢的字不在里面
  新（改动后）= 麦克风全程音频  → 丢的字还在里面

    python tools/probe_all.py
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
WAV = (ROOT / "models" / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
       / "test_wavs" / "rap1.wav")


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        return (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                .astype(np.float32) / 32768.0)


def loudest(x: np.ndarray) -> int:
    frame = int(SR * 0.02)
    n = len(x) // frame
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    lo, hi = int(n * 0.2), int(n * 0.8)
    return (lo + int(np.argmax(rms[lo:hi]))) * frame


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


def run(label: str, audio: np.ndarray) -> None:
    f = Fake(dict(CONFIG.get("vad") or {}))
    f.rec.start()
    f.feed(audio)
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
    total = len(audio)
    print(f"\n=== {label} ===")
    print(f"  输入          {total/SR:6.2f}s")
    print(f"  旧 VAD 段拼接 {len(old)/SR:6.2f}s   缺 {(total-len(old))/SR*1000:+7.0f}ms")
    print(f"  新 全程缓冲   {len(new)/SR:6.2f}s   缺 {(total-len(new))/SR*1000:+7.0f}ms")
    print(f"  VAD 段数 {len(f.segs)}: {[f'{len(s)/SR:.2f}s' for s in f.segs]}")


def main() -> None:
    x = load(WAV)
    a = loudest(x)
    speech_a = x[max(0, a - SR * 2): a + SR * 1]        # 前一段话，约 3s
    gap = np.zeros(int(SR * 0.7), dtype=np.float32)
    speech_b = x[a + SR * 3: a + SR * 5]                # 后一段话，约 2s

    for ms in (200, 300, 400):
        half = int(SR * ms / 1000) // 2
        word = x[max(0, a + SR - half): a + SR + half]   # 一个孤立短字
        audio = np.concatenate([speech_a, gap, word, gap, speech_b])
        run(f"中间插入 {ms}ms 孤立语音（前后各 700ms 停顿）", audio)


if __name__ == "__main__":
    main()
