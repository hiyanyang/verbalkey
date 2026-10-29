"""验证「麦克风音量低」会不会明显拉低准确率，以及自动增益能不能救回来。

做法：把同一段音频衰减 20dB（模拟小音量麦克风），分别在
「原样送识别」和「先归一化再送」两种情况下算 CER。
如果归一化能把 CER 拉回来，说明 AGC 值得做进主链路。
"""

from __future__ import annotations

import re
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.ui  # noqa: F401

from src.asr import AsrEngine  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
WAV_DIR = ROOT / "logs" / "asr_eval"


def norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s or "")


def cer(ref: str, hyp: str) -> float:
    r, h = list(ref), list(hyp)
    n, m = len(r), len(h)
    g = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        g[i][0] = i
    for j in range(1, m + 1):
        g[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            g[i][j] = min(g[i - 1][j - 1] + (0 if r[i - 1] == h[j - 1] else 1),
                          g[i - 1][j] + 1, g[i][j - 1] + 1)
    return g[n][m] / max(1, n)


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def agc(x: np.ndarray, target: float = 0.85, max_gain: float = 6.0) -> np.ndarray:
    """把峰值拉到 target。低电平才动手，已经够响的不放大。"""
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak < 0.005:            # 基本是静音，别把底噪放大
        return x
    gain = min(target / peak, max_gain)
    if gain <= 1.05:
        return x
    return np.clip(x * gain, -1.0, 1.0)


def main() -> int:
    refs = {}
    for line in (MODEL_DIR / "test_wavs" / "transcript.txt").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            k, _, v = line.strip().partition(" ")
            refs[k] = v.strip()

    eng = AsrEngine()
    eng.load(MODEL_DIR, int(CONFIG.get("asr", "num_threads")), "",
             CONFIG.get("asr", "provider"))
    print("模型就绪", flush=True)

    for name, fname in (("fast1", "fast1.16k.wav"),
                        ("raokouling", "raokouling.16k.wav")):
        x = read_wav(WAV_DIR / fname)
        ref = refs[name + ".wav"]
        peak0 = float(np.max(np.abs(x)))
        print(f"\n=== {name}  原始峰值 {peak0:.2f} ===")

        for db in (0.0, -20.0, -30.0):
            y = x * (10 ** (db / 20.0))
            pk = float(np.max(np.abs(y)))
            t1 = eng.transcribe(y)
            t2 = eng.transcribe(agc(y))
            g = 0.85 / pk if pk > 0.005 else 1.0
            print(f"  衰减 {db:>5.1f}dB (峰值 {pk:.3f}, AGC x{g:.1f})")
            print(f"    原样送  CER {cer(norm(ref), norm(t1)) * 100:5.1f}%")
            print(f"    归一化  CER {cer(norm(ref), norm(t2)) * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
