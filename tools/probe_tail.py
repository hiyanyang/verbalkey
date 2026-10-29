"""查「每句最后一个字丢」的根因：尾音到底被削掉多少。

只做测量，不改任何行为。

    python tools/probe_tail.py           # 只看包络与削尾量
    python tools/probe_tail.py --asr     # 再做端到端解码对比（慢，约 1 分钟）
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 本机系统 python 没装 sherpa_onnx，直接从打包产物里借（PyInstaller onedir 的 _internal）
sys.path.insert(0, str(ROOT / "dist" / "Verbalkey" / "_internal"))

from src.audio import _trim_silence  # noqa: E402

SR = 16000
FRAME = 0.02          # 20ms 一帧，和 _trim_silence 一致
KEEP = 0.20           # _trim_silence 的 keep_sec


def load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        assert w.getframerate() == SR, f"{p} 不是 16k"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def envelope(x: np.ndarray, frame_sec: float = FRAME):
    step = int(SR * frame_sec)
    n = len(x) // step
    return np.sqrt(np.mean(x[:n * step].reshape(n, step) ** 2, axis=1))


def analyse(name: str, x: np.ndarray):
    rms = envelope(x)
    peak = float(rms.max())
    thr = max(0.004, peak * 0.08)      # _trim_silence 的规则
    abs_thr = 0.008                     # Recorder.SILENCE_RMS

    idx = np.nonzero(rms > thr)[0]
    last_voiced = idx[-1] if len(idx) else 0
    idx_abs = np.nonzero(rms > abs_thr)[0]
    last_abs = idx_abs[-1] if len(idx_abs) else 0

    # 复算 _trim_silence 的切点，单独看首尾各削多少
    step = int(SR * FRAME)
    n = len(x) // step
    keep = int(SR * KEEP)
    start = max(0, idx[0] * step - keep)
    end = min(len(x), (idx[-1] + 1) * step + keep)
    head_cut = start / SR
    tail_cut = max(0.0, len(x) / SR - end / SR)

    print(f"\n=== {name} ===  总长 {len(x)/SR:.2f}s")
    print(f"峰值帧 RMS {peak:.4f}   相对阈值 peak*8% = {thr:.4f}   绝对阈值 {abs_thr}")
    print(f"最后一个「相对阈值」有声帧 @ {(last_voiced+1)*FRAME:.2f}s")
    print(f"最后一个「绝对阈值」有声帧 @ {(last_abs+1)*FRAME:.2f}s"
          f"   差 {(last_abs-last_voiced)*FRAME*1000:+.0f}ms  ← 这段是弱尾音，被当静音")
    print("最后 1 秒 RMS（20ms 一格）：X=高于相对阈值  .=弱音  ␣=真静音")
    line = "".join("X" if v > thr else ("." if v > abs_thr else " ") for v in rms[-50:])
    print(f"  [{line}]")
    kept = _trim_silence(x, SR)
    print(f"实测 _trim_silence：{len(x)/SR:.2f}s → {len(kept)/SR:.2f}s"
          f"  削首 {head_cut*1000:.0f}ms  削尾 {tail_cut*1000:.0f}ms"
          f"（其中回补 {KEEP*1000:.0f}ms）")


def asr_compare(name: str, x: np.ndarray):
    import sherpa_onnx

    from src.config import MODELS_DIR

    md = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
    rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(md / "conv_frontend.onnx"),
        encoder=str(md / "encoder.int8.onnx"),
        decoder=str(md / "decoder.int8.onnx"),
        tokenizer=str(md / "tokenizer"),
        num_threads=4, provider="cpu", decoding_method="greedy_search",
        hotwords="", max_total_len=1024, max_new_tokens=512,
    )

    def run(a: np.ndarray) -> str:
        st = rec.create_stream()
        st.accept_waveform(SR, np.asarray(a, dtype=np.float32))
        rec.decode_stream(st)
        return (st.result.text or "").strip()

    print(f"\n--- 端到端：{name} ---")
    cases = [("原始", x)]
    cases.append(("切尾150ms", x[:len(x) - int(SR * 0.15)]))
    for ms in (500,):
        cases.append((f"补尾{ms}ms", np.concatenate(
            [x, np.zeros(int(SR * ms / 1000), dtype=np.float32)])))
    for label, a in cases:
        t = run(a)
        print(f"  {label:<12} {len(a)/SR:5.2f}s  末12字「{t[-12:]}」")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    do_asr = "--asr" in sys.argv

    files = [(p.stem, p) for p in sorted(
        (ROOT / "logs" / "asr_eval").glob("*.wav"))]
    if not files:
        md = MODELS_DIR_FALLBACK()
        files = [("fast1", md / "fast1.wav")]
    if args:
        files = [f for f in files if args[0] in f[0]]
    for name, p in files:
        x = load_wav(p)
        analyse(name, x)
        if do_asr:
            asr_compare(name, x)


def MODELS_DIR_FALLBACK() -> Path:
    from src.config import MODELS_DIR
    return MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"


if __name__ == "__main__":
    main()
