"""决定性对比：重解用的音频 vs 原始音频，识别结果差在哪。

链路里整段重解用的是 Recorder._utterance —— 那是「VAD 分段 + 每段削首尾
静音」之后拼起来的，已经不是原始音频了。这里把两份音频分别解码，看字少没少。

    python tools/probe_tail3.py
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

import sherpa_onnx  # noqa: E402

from src.audio import Recorder  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        assert w.getframerate() == SR
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def build():
    md = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
    return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(md / "conv_frontend.onnx"),
        encoder=str(md / "encoder.int8.onnx"),
        decoder=str(md / "decoder.int8.onnx"),
        tokenizer=str(md / "tokenizer"),
        num_threads=4, provider="cpu", decoding_method="greedy_search",
        hotwords="", max_total_len=1024, max_new_tokens=512,
    )


def decode(rec, a: np.ndarray) -> str:
    st = rec.create_stream()
    st.accept_waveform(SR, np.asarray(a, dtype=np.float32))
    rec.decode_stream(st)
    return (st.result.text or "").strip()


def simulate(x: np.ndarray, vad_cfg: dict, tail_ms: int = 300) -> np.ndarray:
    """走一遍真实录音链路，返回重解会用到的那段音频。"""
    rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, vad_cfg,
                   on_segment=lambda *a: None, on_error=lambda m: None)
    rec.open = lambda: True
    rec.start()
    audio = x if not tail_ms else np.concatenate(
        [x, np.zeros(int(SR * tail_ms / 1000), dtype=np.float32)])
    for i in range(0, len(audio) - BLK, BLK):
        rec._callback(audio[i:i + BLK].reshape(-1, 1), BLK, None, None)
    rec.stop()
    t0 = time.time()
    while rec._worker and rec._worker.is_alive():
        time.sleep(0.02)
        if time.time() - t0 > 5:
            break
    u = rec.take_utterance()
    return u if u is not None else np.zeros(0, dtype=np.float32)


def main():
    cfg = dict(CONFIG.get("vad") or {})
    rec = build()
    for p in sorted((ROOT / "logs" / "asr_eval").glob("*.wav")):
        x = load(p)
        utt = simulate(x, cfg)
        print(f"\n=== {p.stem} ===  原始 {len(x)/SR:.2f}s → 重解音频 {len(utt)/SR:.2f}s")
        a = decode(rec, x)
        b = decode(rec, utt)
        print(f"  原始整段 : {a}")
        print(f"  重解音频 : {b}")
        print(f"  字数 {len(a)} vs {len(b)}   差 {len(b)-len(a):+d}")
        if a != b:
            # 找出不同的尾部
            import difflib
            sm = difflib.SequenceMatcher(None, a, b)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    print(f"    [{tag}] 原始「{a[i1:i2]}」→ 重解「{b[j1:j2]}」")
        else:
            print("  完全一致")


if __name__ == "__main__":
    main()
