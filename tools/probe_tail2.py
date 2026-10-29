"""模拟「说完立刻松手」，看整轮音频在链路里少了多少。

不走真实麦克风：直接拿 wav 当输入喂给 Recorder._callback，然后按真实
时序松手，最后看 take_utterance() 拿到多少、跟原音频差多少。

    python tools/probe_tail2.py
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dist" / "Verbalkey" / "_internal"))

from src.audio import Recorder, build_vad  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

SR = 16000
BLK = 512


def load(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        assert w.getframerate() == SR
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class Fake:
    """包一层，记录所有送出去识别的段，并跳过真实麦克风。"""

    def __init__(self, vad_cfg):
        vad_model = str(MODELS_DIR / "silero_vad.onnx")
        self.segs: list[np.ndarray] = []
        self.rec = Recorder(vad_model, SR, vad_cfg,
                            on_segment=self._on_seg,
                            on_error=lambda m: print("  [err]", m))
        self.rec.open = lambda: True          # 不碰真麦克风

    def _on_seg(self, samples, forced, gap):
        self.segs.append(np.asarray(samples, dtype=np.float32))

    def feed(self, x: np.ndarray):
        for i in range(0, len(x) - BLK, BLK):
            self.rec._callback(x[i:i + BLK].reshape(-1, 1), BLK, None, None)


def run_case(name: str, audio: np.ndarray, tail_silence_ms: int,
             vad_cfg: dict):
    f = Fake(vad_cfg)
    f.rec.start()
    # 预缓冲里是空的，直接喂正片
    f.feed(audio)
    if tail_silence_ms:
        f.feed(np.zeros(int(SR * tail_silence_ms / 1000), dtype=np.float32))

    # 松手前：累积缓冲里还剩多少（这段最容易在收尾时被扔掉）
    acc_len = sum(len(p) for p in f.rec._acc)
    n_pushed = len(f.segs)

    # 给 _push 打点，看松手那一刻到底送出什么
    pushes: list[tuple[int, bool]] = []
    orig_push = f.rec._push

    def spy_push(samples, forced=False, gap=0):
        r = orig_push(samples, forced=forced, gap=gap)
        pushes.append((len(samples), bool(forced)))
        return r

    f.rec._push = spy_push

    # 给 vad.flush 打点，看它吐出几段、多长
    flush_parts: list[int] = []
    orig_flush = f.rec.vad.flush

    def spy_flush():
        r = orig_flush()
        after = []
        while not f.rec.vad.empty():
            seg = f.rec.vad.front
            after.append(len(seg.samples))
            f.rec.vad.pop()
        # 塞回去，让真正的 _finish_stop 照样处理
        flush_parts.extend(after)
        return r

    # flush 会把段取走，所以不拦截它，改成事后看 pushes
    t_release = time.time()
    f.rec.stop()
    while f.rec._worker and f.rec._worker.is_alive():
        time.sleep(0.05)
        if time.time() - t_release > 5:
            break
    time.sleep(0.1)
    utt = f.rec.take_utterance()
    got = len(utt) / SR if utt is not None else 0.0
    print(f"\n=== {name} ===  原音频 {len(audio)/SR:.2f}s"
          f"  松手后补静音 {tail_silence_ms}ms")
    print(f"松手瞬间：_acc 里剩 {acc_len/SR:.2f}s   _last_end={f.rec._last_end}"
          f"   已送 {n_pushed} 段")
    print(f"收尾时 _push 了 {len(pushes)} 次："
          f"{[(f'{n/SR:.2f}s', '硬切' if fc else '自然') for n, fc in pushes]}")
    print(f"整轮缓冲 _utterance {got:.2f}s   共丢 {(len(audio)/SR-got)*1000:+.0f}ms")
    print(f"送识别段长: {[f'{len(s)/SR:.2f}s' for s in f.segs]}")


def main():
    cfg = dict(CONFIG.get("vad") or {})
    print("vad 配置:", {k: cfg.get(k) for k in
                       ("threshold", "silence_ms", "min_speech_ms",
                        "max_segment_s", "max_latency_s", "min_asr_sec")})
    files = [p for p in sorted((ROOT / "logs" / "asr_eval").glob("*.wav"))]
    for p in files:
        x = load(p)
        for tail in (0, 300):
            run_case(p.stem, x, tail, cfg)


if __name__ == "__main__":
    main()
