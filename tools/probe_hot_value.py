"""热词到底有没有用：同一段音频，相关词 / 无关词 / 空 三档对照。

判据设计：如果**音频里根本没提过的词**（无关热词）也能把输出明显改掉，
说明热词对生成式 ASR 的作用更接近随机扰动，而不是定向纠偏 —— 那它
就不值得占 prompt 的预算。反过来，如果只有相关热词能改、无关热词不动，
才说明它真的在干活。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.asr import AsrEngine  # noqa: E402
from src.config import MODELS_DIR  # noqa: E402

MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
WAV = ROOT / "logs" / "asr_eval" / "fast1.16k.wav"


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def run(hw: str, samples: np.ndarray) -> str:
    eng = AsrEngine()
    eng.load(MODEL_DIR, 6, hw, "cpu")
    try:
        return eng.transcribe(samples, 16000)
    finally:
        eng.unload()


def main() -> int:
    samples = read_wav(WAV)
    cases = [
        ("空热词（基线）", ""),
        ("相关词：轮胎", "轮胎"),
        ("相关词：轮胎,树杈", "轮胎,树杈"),
        ("无关词：压疮,鼻饲", "压疮,鼻饲"),
        ("当前配置：skill,github,markdown", "skill,github,markdown"),
    ]
    out: list[tuple[str, str]] = []
    for tag, hw in cases:
        text = run(hw, samples)
        out.append((tag, text))
        print(f"\n[{tag}]  {len(text)} 字")
        print(f"  {text}")

    print("\n—— 与基线逐字差异 ——")
    base = out[0][1]
    for tag, text in out[1:]:
        same = text == base
        hit = {w: ("有" if w in text else "无")
               for w in ("轮胎", "树杈", "压疮", "鼻饲")}
        print(f"\n[{tag}] {'完全相同' if same else '有改动'}")
        print(f"  关键词: {hit}")
        if not same:
            import difflib
            sm = difflib.SequenceMatcher(None, base, text, autojunk=False)
            ratio = sm.ratio()
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op != "equal":
                    print(f"  {op}: {base[i1:i2]!r} → {text[j1:j2]!r}")
            print(f"  整段相似度 {ratio:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
