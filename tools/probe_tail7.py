"""尾部补静音到底补多少才有效：对整段重解做参数扫描。

    python tools/probe_tail7.py
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

import src.asr as A  # noqa: E402
from src.audio import Recorder, _trim_silence  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512
MODEL = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def record(y: np.ndarray) -> np.ndarray:
    segs: list[np.ndarray] = []
    rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR,
                   dict(CONFIG.get("vad") or {}),
                   on_segment=lambda s, f, g: segs.append(np.asarray(s, np.float32)),
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
    return rec.take_utterance()


def main():
    eng = A.AsrEngine()
    eng.load(MODEL, num_threads=4)
    for name in ("fast1.16k", "raokouling.16k"):
        p = ROOT / "logs" / "asr_eval" / f"{name}.wav"
        if not p.exists():
            continue
        utt = record(load(p))
        clean = _trim_silence(utt, SR)
        print(f"\n=== {name}  整轮 {len(utt)/SR:.2f}s → 削两头 {len(clean)/SR:.2f}s")
        for pad in (0.0, 0.4, 0.8, 1.2, 1.6):
            A._TAIL_PAD_SEC = pad
            t = time.time()
            txt = eng.transcribe(clean, SR)
            print(f"  补 {pad:3.1f}s  {time.time()-t:5.1f}s  {len(txt):3d}字 "
                  f"末12字「{txt[-12:]}」")


if __name__ == "__main__":
    main()
