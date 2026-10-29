"""用真实 VAD 跑长音频，看新参数下实际切出什么段、准确率多少。

和 eval_asr.py 的区别：那边是「人为按固定秒数硬切」，这边跑的是
真实的 Recorder 逻辑（VAD 端点 + 短段合并 + 强制切分），也就是
用户说话时实际会发生的事。
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
from src.audio import Recorder  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

WAV_DIR = ROOT / "logs" / "asr_eval"
MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
REF_FILE = MODEL_DIR / "test_wavs" / "transcript.txt"


def norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s or "")


def cer(ref: str, hyp: str) -> float:
    r, h = list(ref), list(hyp)
    n, m = len(r), len(h)
    grid = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        grid[i][0] = i
    for j in range(1, m + 1):
        grid[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            grid[i][j] = min(grid[i - 1][j - 1] + (0 if r[i - 1] == h[j - 1] else 1),
                             grid[i - 1][j] + 1,
                             grid[i][j - 1] + 1)
    return grid[n][m] / max(1, n)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def load_refs() -> dict[str, str]:
    out = {}
    if not REF_FILE.exists():
        return out
    for line in REF_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, _, text = line.strip().partition(" ")
        out[name] = text.strip()
    return out


def cut_with_real_vad(samples: np.ndarray,
                      cfg: dict | None = None) -> list[tuple[np.ndarray, bool]]:
    rec = Recorder(
        vad_model=str(MODELS_DIR / "silero_vad.onnx"),
        sample_rate=16000,
        vad_cfg=cfg or CONFIG.get("vad"),
    )
    rec._accepting = True
    blk = 512
    for i in range(0, len(samples) - blk + 1, blk):
        rec._callback(samples[i:i + blk].reshape(-1, 1), blk, None, None)
    rec._finish_stop()

    out = []
    while not rec._segments.empty():
        out.append(rec._segments.get())
    return out


def run(eng: AsrEngine, name: str, ref: str, tag: str,
        cfg: dict | None = None) -> None:
    samples = read_wav(WAV_DIR / f"{name}.16k.wav")
    dur = len(samples) / 16000.0
    parts = cut_with_real_vad(samples, cfg)
    print(f"\n=== {name} [{tag}]  {dur:.1f}s  → 切出 {len(parts)} 段 ===")

    texts = []
    for k, (seg, forced) in enumerate(parts, 1):
        t = eng.transcribe(seg)
        # 硬切段不是句尾，句号换成逗号（和 main._decode_segment 一致）
        if forced and t.endswith("。"):
            t = t[:-1] + "，"
        texts.append(t)
        mark = "硬切" if forced else "句尾"
        print(f"  {k}. {len(seg)/16000:4.1f}s {mark}  {t}")

    joined = "".join(texts)
    print(f"  CER {cer(norm(ref), norm(joined)) * 100:.1f}%")


def main() -> int:
    refs = load_refs()
    eng = AsrEngine()
    print("加载模型…", flush=True)
    eng.load(MODELS_DIR / CONFIG.get("asr", "model_id"),
             int(CONFIG.get("asr", "num_threads")), "",
             CONFIG.get("asr", "provider"))
    print("就绪", flush=True)
    print("vad =", CONFIG.get("vad"))

    # 用户改之前的参数：322ms 就断句、1 秒才算有效、每 5 秒硬切、
    # 不做短段合并。复现出来才说得清「到底改好了多少」。
    old = dict(CONFIG.get("vad"))
    old.update({"silence_ms": 322, "min_speech_ms": 1000,
                "max_segment_s": 12, "max_latency_s": 5, "min_asr_sec": 0})

    # 候选：只把断句阈值和短段合并改掉，硬切仍保持 5 秒
    mid = dict(CONFIG.get("vad"))
    mid.update({"max_latency_s": 5, "max_segment_s": 10})

    # 450ms 断句 + 3 秒短段合并：VAD 在自然停顿处切，碎片靠合并兜住
    soft = dict(CONFIG.get("vad"))
    soft.update({"silence_ms": 450, "max_latency_s": 6, "max_segment_s": 10})

    for name in ("fast1", "raokouling"):
        ref = refs.get(f"{name}.wav")
        if not ref:
            print(f"没有 {name} 的参考文本，跳过")
            continue
        run(eng, name, ref, "改前", old)
        run(eng, name, ref, "600/5s", mid)
        run(eng, name, ref, "450/6s", soft)
    return 0


if __name__ == "__main__":
    sys.exit(main())
