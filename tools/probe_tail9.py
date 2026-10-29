"""在 4 段中文音频上把「尾部补静音长度」钉死（pad 0 / 0.2 / 0.4 / 0.6）。

    python tools/probe_tail9.py
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
sys.path.insert(0, str(ROOT / "tools"))

import src.asr as A  # noqa: E402
from eval_asr import cer, load_refs, norm  # noqa: E402
from src.audio import Recorder, _trim_silence  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512
MODEL = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
TW = MODEL / "test_wavs"
PADS = (0.0, 0.2, 0.4, 0.6)
FILES = [ROOT / "logs" / "asr_eval" / "fast1.16k.wav",
         ROOT / "logs" / "asr_eval" / "raokouling.16k.wav",
         TW / "noise2.wav",
         TW / "cantonese.wav"]


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def record(y: np.ndarray) -> np.ndarray:
    rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR,
                   dict(CONFIG.get("vad") or {}),
                   on_segment=lambda s, f, g: None,
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
    A._TAIL_PAD_SEC = 0.0
    eng = A.AsrEngine()
    eng.load(MODEL, num_threads=4)
    refs = {k.replace(".wav", ""): v for k, v in load_refs().items()}
    rows = []
    for p in FILES:
        if not p.exists():
            print("缺文件", p)
            continue
        key = p.stem.split(".")[0]
        ref = norm(refs.get(key, ""))
        if not ref:
            print("没参考文本，跳过", p.name)
            continue
        clean = _trim_silence(record(load(p)), SR)
        print(f"\n=== {p.name}  {len(clean)/SR:.2f}s  参考 {len(ref)} 字")
        for pad in PADS:
            x = np.concatenate([clean, np.zeros(int(SR * pad), np.float32)])
            t = time.time()
            hyp = norm(eng.transcribe(x, SR))
            dt = time.time() - t
            e, s, d, i = cer(ref, hyp)
            print(f"  补 {pad:3.1f}s  CER {e*100:5.1f}%  替{s:3d} 删{d:3d} 插{i:3d} "
                  f"{dt:5.1f}s")
            rows.append((pad, e, d))
    print("\n--- 汇总（4 段） ---")
    for pad in PADS:
        t = [r for r in rows if r[0] == pad]
        if not t:
            continue
        print(f"  补 {pad:3.1f}s  平均 CER {sum(r[1] for r in t)/len(t)*100:5.1f}%  "
              f"总删 {sum(r[2] for r in t):3d}")


if __name__ == "__main__":
    main()
