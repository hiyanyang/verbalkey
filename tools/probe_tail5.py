"""造出「用户每句停顿 700ms」的音频，验证自然断句路径下句尾丢字。

前面的探针全是「一口气说不停」→ 全走 3 秒硬切，跟真实口述不一样。
这里在能量最低处插入 700ms 静音，让 VAD 真的自己断句，然后看：
  1. VAD 自然切出的段，段尾还剩多少静音
  2. 同一段，削过 vs 原始，最后一个字识别成什么

    python tools/probe_tail5.py            # 只看段尾静音
    python tools/probe_tail5.py --asr      # 连识别一起
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
PAUSE_MS = 700


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def rms_env(x: np.ndarray, frame: int = 320) -> np.ndarray:
    n = len(x) // frame
    return np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))


def insert_pauses(x: np.ndarray, every: float = 3.2) -> tuple[np.ndarray, list]:
    """每隔 every 秒，在附近能量最低处插 700ms 静音。

    切在能量谷而不是硬切点上 —— 硬切会切进字中间，那段音频本身就不完整，
    测出来的丢字分不清是切坏的还是识别坏的。
    """
    env = rms_env(x)
    frame = 320
    parts, marks, prev = [], [], 0
    t = every
    while t < len(x) / SR - 0.5:
        c = int(t * SR) // frame
        lo, hi = max(0, c - int(0.5 * SR) // frame), min(len(env), c + int(0.5 * SR) // frame)
        if hi <= lo:
            break
        k = lo + int(np.argmin(env[lo:hi]))
        pos = k * frame
        if pos > prev + int(0.5 * SR):
            parts.append(x[prev:pos])
            parts.append(np.zeros(int(SR * PAUSE_MS / 1000), dtype=np.float32))
            marks.append(pos / SR)
            prev = pos
        t += every
    parts.append(x[prev:])
    return np.concatenate(parts), marks


def tail_ms(x: np.ndarray, thr: float = 0.004) -> float:
    frame = int(SR * 0.02)
    n = len(x) // frame
    if n < 1:
        return 0.0
    e = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    cnt = 0
    for v in e[::-1]:
        if v > thr:
            break
        cnt += 1
    return cnt * 20.0


def main():
    do_asr = "--asr" in sys.argv
    cfg = dict(CONFIG.get("vad") or {})
    p = ROOT / "logs" / "asr_eval" / "fast1.16k.wav"
    x = load(p)
    y, marks = insert_pauses(x)
    print(f"原 {len(x)/SR:.2f}s → 插入 {len(marks)} 处静音后 {len(y)/SR:.2f}s")

    segs: list[np.ndarray] = []
    forced_flags: list[bool] = []
    rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, cfg,
                   on_segment=lambda s, f, g: (segs.append(np.asarray(s, np.float32)),
                                               forced_flags.append(f)),
                   on_error=lambda m: print("  [err]", m))
    rec.open = lambda: True
    rec.start()
    for i in range(0, len(y) - BLK, BLK):
        rec._callback(y[i:i + BLK].reshape(-1, 1), BLK, None, None)
    rec.stop()
    t0 = time.time()
    while rec._worker and rec._worker.is_alive() and time.time() - t0 < 8:
        time.sleep(0.05)
    time.sleep(0.2)
    utt = rec.take_utterance()

    print(f"\nVAD 切出 {len(segs)} 段（{'硬切' if any(forced_flags) else '全部自然断句'}）")
    for i, s in enumerate(segs):
        print(f"  段{i} {len(s)/SR:5.2f}s  尾静音 {tail_ms(s):4.0f}ms  "
              f"{'硬切' if forced_flags[i] else '自然'}")
    if utt is not None:
        print(f"整段缓冲 {len(utt)/SR:.2f}s  尾静音 {tail_ms(utt):.0f}ms")

    if not do_asr:
        return
    from src.asr import AsrEngine
    eng = AsrEngine()
    eng.load(MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25", num_threads=4)
    print("\n每段单独解码（这就是草稿路径）:")
    for i, s in enumerate(segs):
        if len(s) < 0.3 * SR:
            continue
        a = eng.transcribe(_trim_silence(s, SR), SR)
        b = eng.transcribe(s, SR)
        c = eng.transcribe(np.concatenate([s, np.zeros(int(0.4 * SR), np.float32)]), SR)
        print(f"  段{i} 削过「{a[-8:]}」 | 原始「{b[-8:]}」 | 原始+补400ms「{c[-8:]}」")


if __name__ == "__main__":
    main()
