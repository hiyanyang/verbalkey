"""对比 greedy_search 与 modified_beam_search 的准确率。

Qwen3-ASR 是 LLM 解码器，beam search 未必支持、也未必更准，
所以这里只做一件事：同一批音频、同一套参数，只换解码方式，看 CER。
"""

from __future__ import annotations

import re
import sys
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.ui  # noqa: F401

from src.asr import AsrModel  # noqa: E402
from src.config import MODELS_DIR  # noqa: E402

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


def build(method: str):
    m = AsrModel(MODEL_DIR)
    return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(m.conv_frontend),
        encoder=str(m.encoder),
        decoder=str(m.decoder),
        tokenizer=str(m.tokenizer),
        num_threads=6,
        provider="cpu",
        decoding_method=method,
        hotwords="",
        max_total_len=1024,
        max_new_tokens=512,
    )


def main() -> int:
    refs = {}
    for line in (MODEL_DIR / "test_wavs" / "transcript.txt").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            k, _, v = line.strip().partition(" ")
            refs[k] = v.strip()

    cases = [("fast1", "fast1.16k.wav"), ("raokouling", "raokouling.16k.wav")]

    for method in ("greedy_search", "modified_beam_search"):
        print(f"\n===== {method} =====", flush=True)
        try:
            rec = build(method)
        except Exception as e:
            print(f"  构造失败：{type(e).__name__}: {e}")
            continue
        for name, fname in cases:
            samples = read_wav(WAV_DIR / fname)
            st = rec.create_stream()
            st.accept_waveform(16000, samples)
            rec.decode_stream(st)
            out = (st.result.text or "").strip()
            print(f"  {name:12s} CER {cer(norm(refs[name + ".wav"]), norm(out)) * 100:5.1f}%")
            print(f"    {out[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
