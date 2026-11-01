"""量一下热词到底让识别慢多少。

做法：同一段音频、同一套参数，只改 hotwords 个数（0 / 3 / 8），
每个配置跑 3 轮取**最小值**当纯计算时间（最小值最接近真实开销，
平均值会被系统抖动污染）。

模型重建不计入耗时 —— 那是启动时一次性成本，跟每句话的延迟无关。
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.asr import AsrEngine, AsrModel  # noqa: E402
from src.config import MODELS_DIR  # noqa: E402

MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
WAV = ROOT / "logs" / "asr_eval" / "fast1.16k.wav"
THREADS = 6
ROUNDS = 3


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    samples = read_wav(WAV)
    secs = len(samples) / 16000.0
    print(f"音频 {WAV.name}  {secs:.2f}s  线程 {THREADS}  每档 {ROUNDS} 轮\n")

    cases = [
        ("0 个热词（空）", ""),
        ("3 个（当前配置）", "skill,github,markdown"),
        ("8 个（上限）", "skill,github,markdown,压疮,鼻饲,翻身扣背,陪护,护工"),
    ]

    rows = []
    for tag, hw in cases:
        eng = AsrEngine()
        eng.load(MODEL_DIR, THREADS, hw, "cpu")
        times = []
        text = ""
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            text = eng.transcribe(samples, 16000)
            times.append(time.perf_counter() - t0)
        eng.unload()
        best, avg = min(times), sum(times) / len(times)
        rows.append((tag, best, avg, len(text)))
        print(f"[{tag}]")
        print(f"  各轮: {' '.join(f'{t:.3f}' for t in times)}")
        print(f"  最快 {best:.3f}s  平均 {avg:.3f}s  输出 {len(text)} 字")
        print(f"  {text[:70]}\n")

    base = rows[0][1]
    print("—— 相对「0 个热词」的增量（按最快一轮算）——")
    for tag, best, _avg, n in rows[1:]:
        print(f"  {tag}: {best - base:+.3f}s  ({(best / base - 1) * 100:+.1f}%)  输出 {n} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
