"""端到端 A/B：改动前 vs 改动后，看终稿的末句是不是对上了官方转写。

改动前 = 每段削过静音拼接做整段重解 + 解码不补尾巴
改动后 = 原始音频（整轮）削两头 + 解码补 400ms

    python tools/probe_tail6.py
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


def record(y: np.ndarray):
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
    return segs, rec.take_utterance()


def main():
    eng = A.AsrEngine()
    eng.load(MODEL, num_threads=4)
    official = {}
    for line in (MODEL / "test_wavs" / "transcript.txt").read_text(
            encoding="utf-8").splitlines():
        if " " in line:
            k, v = line.split(" ", 1)
            official[k.replace(".wav", "")] = v.strip()

    for name in ("fast1.16k", "raokouling.16k"):
        p = ROOT / "logs" / "asr_eval" / f"{name}.wav"
        if not p.exists():
            continue
        x = load(p)
        segs, utt = record(x)
        print(f"\n=== {name}  {len(x)/SR:.2f}s  "
              f"官方末10字「{official.get(name.split('.')[0], '')[-10:]}」")

        # --- 改动后：原始音频，削两头，解码补 400ms ---
        A._TAIL_PAD_SEC = 0.40
        new_draft = "".join(eng.transcribe(s, SR) for s in segs)
        new_final = eng.transcribe(_trim_silence(utt, SR), SR)

        # --- 改动前：分段拼接（已削过），解码不补 ---
        A._TAIL_PAD_SEC = 0.0
        old_utt = np.concatenate(segs) if len(segs) > 1 else segs[0]
        old_draft = "".join(eng.transcribe(s, SR) for s in segs)
        old_final = eng.transcribe(old_utt, SR)

        for label, d, f in (("改动前", old_draft, old_final),
                            ("改动后", new_draft, new_final)):
            print(f"  {label} 草稿 {len(d):3d}字「{d[-10:]}」")
            print(f"  {label} 终稿 {len(f):3d}字「{f[-10:]}」")


if __name__ == "__main__":
    main()
