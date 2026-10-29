"""尾部补静音的长度用 CER 分解来定，而不是看「读起来像不像」。

关键指标是 CER 里的**删除数**：它等于模型听漏的字数，正是「每句最后一个字
丢了」这件事的量化。

    python tools/probe_tail8.py
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
PADS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


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
    A._TAIL_PAD_SEC = 0.0        # 补齐由本脚本自己做，别叠两次
    eng = A.AsrEngine()
    eng.load(MODEL, num_threads=4)
    refs = {k.replace(".wav", ""): v for k, v in load_refs().items()}
    rows = []
    for name in ("fast1.16k", "raokouling.16k"):
        p = ROOT / "logs" / "asr_eval" / f"{name}.wav"
        if not p.exists():
            continue
        clean = _trim_silence(record(load(p)), SR)
        ref = norm(refs[name.split(".")[0]])
        print(f"\n=== {name}  {len(clean)/SR:.2f}s  参考 {len(ref)} 字")
        for pad in PADS:
            x = np.concatenate(
                [clean, np.zeros(int(SR * pad), dtype=np.float32)])
            t = time.time()
            hyp = norm(eng.transcribe(x, SR))
            dt = time.time() - t
            e, s, d, i = cer(ref, hyp)
            print(f"  补 {pad:3.1f}s  CER {e*100:5.1f}%  替{s:3d} 删{d:3d} 插{i:3d}  "
                  f"{dt:5.1f}s  {len(hyp):3d}字")
            rows.append((name, pad, e, d, dt))
    print("\n--- 按 pad 汇总 ---")
    for pad in PADS:
        tot = [r for r in rows if r[1] == pad]
        if not tot:
            continue
        print(f"  补 {pad:3.1f}s  平均 CER {sum(r[2] for r in tot)/len(tot)*100:5.1f}%  "
              f"总删 {sum(r[3] for r in tot):3d}  平均耗时 {sum(r[4] for r in tot)/len(tot):5.1f}s")


if __name__ == "__main__":
    main()
