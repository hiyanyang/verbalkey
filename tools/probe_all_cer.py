"""全程缓冲 vs VAD 段拼接：用 CER 量化，不靠肉眼数末字。

同一段音频走真实 Recorder，拿两种口径分别解码，跟官方转写比编辑距离。
CER = (替换 + 删除 + 插入) / 参考字数。删除数就是「丢字」的个数。

    python tools/probe_all_cer.py
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

from src.audio import Recorder  # noqa: E402
from src.asr import AsrEngine  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512
WAVS = ROOT / "models" / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25" / "test_wavs"


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        return (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                .astype(np.float32) / 32768.0)


def cer(ref: str, hyp: str) -> tuple[float, int, int, int]:
    """返回 (cer, 替换, 删除, 插入)。删除数 = 丢了多少字。"""
    r, h = list(ref), list(hyp)
    n, m = len(r), len(h)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = r[i - 1] == h[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (0 if same else 1))
    # 回溯分类
    i, j, rep, dele, ins = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (0 if r[i - 1] == h[j - 1] else 1):
            if r[i - 1] != h[j - 1]:
                rep += 1
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            dele += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return d[n][m] / max(n, 1), rep, dele, ins


class Fake:
    def __init__(self, vad_cfg):
        self.segs: list[np.ndarray] = []
        self.rec = Recorder(str(MODELS_DIR / "silero_vad.onnx"), SR, vad_cfg,
                            on_segment=self._on_seg,
                            on_error=lambda m: print("  [err]", m))
        self.rec.open = lambda: True

    def _on_seg(self, samples, forced, gap):
        self.segs.append(np.asarray(samples, dtype=np.float32))

    def feed(self, x: np.ndarray):
        for i in range(0, len(x) - BLK, BLK):
            self.rec._callback(x[i:i + BLK].reshape(-1, 1), BLK, None, None)


def main() -> None:
    refs = {}
    for line in (WAVS / "transcript.txt").read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition(" ")
        refs[k] = v.strip()

    texts = {}
    # logs/asr_eval 里是已经转成 16k 单声道的同一批音频
    for stem, ref_key in (("fast1.16k", "fast1.wav"),
                          ("raokouling.16k", "raokouling.wav")):
        p = ROOT / "logs" / "asr_eval" / f"{stem}.wav"
        if not p.exists():
            print(f"{p.name} 不存在，跳过")
            continue
        x = load(p)
        if not refs.get(ref_key):
            print(f"没有 {ref_key} 的官方转写，跳过")
            continue
        f = Fake(dict(CONFIG.get("vad") or {}))
        f.rec.start()
        f.feed(x)
        f.rec.stop()
        t0 = time.time()
        while f.rec._worker and f.rec._worker.is_alive():
            time.sleep(0.05)
            if time.time() - t0 > 8:
                break
        time.sleep(0.1)
        new = f.rec.take_utterance()
        old = (np.concatenate(f.segs) if len(f.segs) > 1
               else (f.segs[0] if f.segs else np.zeros(0, dtype=np.float32)))
        texts[stem] = (refs[ref_key], old, new)

    eng = AsrEngine()
    eng.load(MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
             num_threads=4)

    print(f"{'音频':<16}{'口径':<12}{'字数':>5}{'CER':>7}{'替':>4}{'删':>4}{'插':>4}")
    for name, (ref, old, new) in texts.items():
        for label, x in (("旧 VAD 拼接", old), ("新 全程音频", new)):
            txt = eng.transcribe(x, SR)
            c, rep, dele, ins = cer(ref, txt)
            print(f"{name:<16}{label:<12}{len(txt):>5}{c*100:>6.1f}%"
                  f"{rep:>4}{dele:>4}{ins:>4}")


if __name__ == "__main__":
    main()
