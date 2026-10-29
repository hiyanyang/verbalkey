"""验证「句中丢单字」是不是 VAD 的 min_speech_duration 吃掉的。

机制假设：
  用户说话时在某个字前面停了一下（思考、换气），这个字被两侧静音夹成一个
  独立语音段。如果它的时长小于 min_speech_duration（配置里是 400ms），
  silero VAD 直接不认为它是一段语音，于是它既不在分段队列（草稿）里，
  也不在整段缓冲（重解）里 —— 两次识别都没有这个字。

做法：从真实中文音频里截出一小段语音当「一个字」，前后补长静音，喂给
真实的 VAD，看它在不同 min_speech_duration 下认不认这段。

用法：
    python tools/probe_drop.py
"""

from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dist" / "Verbalkey" / "_internal"))
sys.path.insert(0, str(ROOT))

from src.audio import build_vad  # noqa: E402

SR = 16000
MODEL = ROOT / "models" / "silero_vad.onnx"
WAV = (ROOT / "models" / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
       / "test_wavs" / "rap1.wav")   # 16kHz 单声道中文，省掉重采样


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def speech_span(x: np.ndarray) -> tuple[int, int]:
    """在音频中段找能量最高的位置，避开首尾的淡入淡出。"""
    frame = int(SR * 0.02)
    n = len(x) // frame
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    lo, hi = int(n * 0.2), int(n * 0.8)
    idx = lo + int(np.argmax(rms[lo:hi]))
    print(f"取样点 {idx*frame/SR:.2f}s，该处 RMS={rms[idx]:.4f}")
    return idx * frame, idx * frame


def run_vad(audio: np.ndarray, min_speech_ms: int, silence_ms: int = 500) -> list[int]:
    """把音频按 512 块喂给真实 VAD，返回每段语音的长度（采样点）。"""
    cfg = {
        "threshold": 0.5,
        "silence_ms": silence_ms,
        "min_speech_ms": min_speech_ms,
        "max_segment_s": 8,
    }
    vad = build_vad(MODEL, SR, cfg)
    for i in range(0, len(audio), 512):
        vad.accept_waveform(audio[i: i + 512])
        while not vad.empty():
            seg = vad.front
            run_vad.segs.append(len(seg.samples))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = vad.front
        run_vad.segs.append(len(seg.samples))
        vad.pop()
    return run_vad.segs


def case(chunk_ms: int, min_speech_ms: int, pause_ms: int, x: np.ndarray,
         a: int, b: int) -> tuple[bool, float]:
    """取 chunk_ms 的语音，两侧各补 pause_ms 静音，看 VAD 认不认。"""
    half = int(SR * chunk_ms / 1000) // 2
    piece = x[max(0, a - half): a + half]
    pad = np.zeros(int(SR * pause_ms / 1000), dtype=np.float32)
    audio = np.concatenate([pad, piece, pad])
    run_vad.segs = []
    segs = run_vad(audio, min_speech_ms)
    got = sum(segs)
    return got >= len(piece) * 0.5, got / SR


def main() -> None:
    x = load(WAV)
    a, b = speech_span(x)
    print(f"音频 {len(x)/SR:.2f}s，有声区间 {a/SR:.2f}~{b/SR:.2f}s")
    print(f"两侧停顿固定 700ms，中间放一段「单字」长度的语音\n")

    print(f"{'单字时长':<10}", end="")
    for ms in (200, 300, 400):
        print(f"min_speech={ms}ms".ljust(20), end="")
    print()

    for chunk in (150, 200, 250, 300, 400, 500, 700):
        print(f"{chunk}ms".ljust(10), end="")
        for ms in (200, 300, 400):
            ok, sec = case(chunk, ms, 700, x, a, b)
            print((f"{'认得' if ok else '丢'} {sec:.2f}s").ljust(20), end="")
        print()

    print("\n=== 固定 700ms 片段，扫 min_speech 临界点 ===")
    print(f"{'min_speech':<14}{'两侧停顿700ms':<16}{'两侧停顿1000ms':<16}")
    for ms in (100, 150, 200, 250, 300, 400, 500, 600, 700):
        row = f"{ms}ms".ljust(14)
        for pause in (700, 1000):
            ok, sec = case(700, ms, pause, x, a, b)
            row += (f"{'认得' if ok else '丢'} {sec:.2f}s").ljust(16)
        print(row)

    print("\n结论看这一列：min_speech=400 时，多长的语音会被吃掉。")


if __name__ == "__main__":
    main()
