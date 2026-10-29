"""端到端离线验证：WAV 经 VAD 分句 → ASR 识别 → 文本后处理 → 整理层。

不碰麦克风和托盘，只验证链路本身。
用法: python tools/e2e_test.py [wav路径]
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.asr import AsrEngine  # noqa: E402
from src.audio import build_vad  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402
from src.polish import PolishEngine  # noqa: E402
from src.text import process  # noqa: E402


def load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as f:
        sr = f.getframerate()
        ch = f.getnchannels()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    s = data.astype(np.float32) / 32768.0
    if sr != 16000:
        n = int(len(s) * 16000 / sr)
        s = np.interp(np.linspace(0, len(s) - 1, n),
                      np.arange(len(s)), s).astype(np.float32)
    return s


def main():
    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
        / "test_wavs" / "qiqiu1.wav")
    if not wav.exists():
        print(f"找不到音频: {wav}")
        return 1

    samples = load_wav(wav)
    print(f"音频: {wav.name}  {len(samples)/16000:.1f}s")

    # --- 1. ASR ---
    mid = CONFIG.get("asr", "model_id")
    mdir = MODELS_DIR / mid
    asr = AsrEngine()
    t0 = time.time()
    asr.load(mdir, int(CONFIG.get("asr", "num_threads")),
             str(CONFIG.get("asr", "hotwords") or ""), "cpu")
    print(f"[1] 识别模型载入 {time.time()-t0:.2f}s  ({mid})")

    # --- 2. VAD 分句（按 100ms 块喂，模拟实时采集）---
    vad = build_vad(str(MODELS_DIR / "silero_vad.onnx"), 16000, CONFIG.get("vad"))
    chunk = 1600
    segs = []
    for i in range(0, len(samples), chunk):
        vad.accept_waveform(samples[i:i + chunk])
        while not vad.empty():
            seg = vad.front
            segs.append(np.array(seg.samples, dtype=np.float32))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = vad.front
        segs.append(np.array(seg.samples, dtype=np.float32))
        vad.pop()
    print(f"[2] VAD 分出 {len(segs)} 段: "
          + ", ".join(f"{len(s)/16000:.1f}s" for s in segs[:8]))

    # --- 3. 逐句识别 ---
    parts, t_all = [], 0.0
    for i, s in enumerate(segs):
        t = time.time()
        txt = asr.transcribe(s)
        el = time.time() - t
        t_all += el
        parts.append(txt)
        print(f"    {i+1}) {len(s)/16000:.1f}s → {el:.2f}s  {txt}")
        if i >= 7:
            parts.append("…")
            break
    raw = "".join(parts)
    print(f"[3] 合计解码 {t_all:.2f}s，原始转写 {len(raw)} 字")

    # --- 4. 后处理 ---
    final = process(raw, CONFIG.get("text"))
    print(f"[4] 后处理（繁转简/替换）: {final}")

    # --- 5. 整理层 ---
    level = int(CONFIG.get("polish", "level"))
    pe = PolishEngine()
    pe.configure(str(CONFIG.get("polish", "server_bin") or ""),
                 str(CONFIG.get("polish", "model_file") or ""),
                 int(CONFIG.get("polish", "port")),
                 float(CONFIG.get("polish", "idle_unload_min")))
    if not (CONFIG.get("polish", "model_file") and
            Path(str(CONFIG.get("polish", "model_file"))).exists()):
        print("[5] 未配置整理模型，跳过")
        return 0
    t = time.time()
    out, err = pe.polish(final, level)
    print(f"[5] 整理耗时 {time.time()-t:.2f}s，级别 {level}，错误={err}")
    print(f"    整理后: {out}")
    pe.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
