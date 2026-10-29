"""验证 Qwen3-ASR 的 hotwords 是否真的影响输出。

做法：同一段音频、同一串参数，只改 hotwords，看识别出来的词变不变。
如果完全不变，说明这个参数在当前 sherpa-onnx 版本里没接上，
那就不能靠热词修「事变／识别」这类错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.ui  # noqa: F401

from src.asr import AsrEngine  # noqa: E402
from src.asr import AsrModel  # noqa: E402
from src.config import MODELS_DIR  # noqa: E402

MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
WAV = ROOT / "logs" / "asr_eval" / "fast1.16k.wav"


def transcribe(eng, samples):
    st = eng.recognizer.create_stream()
    st.accept_waveform(16000, samples)
    eng.recognizer.decode_stream(st)
    return (st.result.text or "").strip()


def read_wav(path: Path):
    import wave
    import numpy as np
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    samples = read_wav(WAV)
    m = AsrModel(MODEL_DIR)

    cases = [
        ("不加热词", ""),
        ("热词: 树杈,轮胎", "树杈,轮胎"),
        ("热词: 树杈,轮胎,慢动作,树杈,轮胎,慢动作", "树杈,轮胎,慢动作,树杈,轮胎,慢动作"),
    ]
    for tag, hw in cases:
        eng = AsrEngine()
        eng.load(MODEL_DIR, 6, hw, "cpu")
        out = transcribe(eng, samples)
        flags = {w: ("有" if w in out else "无") for w in ("树杈", "轮胎", "慢动作")}
        print(f"\n[{tag}] 长度 {len(out)}")
        print(f"  关键词命中: {flags}")
        print(f"  {out[:90]}")
        eng.unload()

    print("\n注意：长度和用词完全一致 = hotwords 没起到作用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
