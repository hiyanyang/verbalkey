"""量化「切碎」对识别准确率的伤害。

用模型自带的 test_wavs + 官方转写文本做 ground truth，把同一段音频
按不同粒度硬切，分别解码再拼接，算字错误率（CER）。

用法：
    python tools/eval_asr.py            # 跑 fast1 / raokouling
    python tools/eval_asr.py noise2     # 只跑某一条
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import sherpa_onnx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

from src.asr import AsrEngine  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

MODEL_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
WAV_DIR = ROOT / "logs" / "asr_eval"          # 已转好的 16k 单声道
REF_FILE = MODEL_DIR / "test_wavs" / "transcript.txt"


def norm(s: str) -> str:
    """只留汉字、字母、数字，去掉标点和空格，方便逐字比对。"""
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s or "")


def cer(ref: str, hyp: str) -> tuple[float, int, int, int]:
    """字错误率 = (替换 + 删除 + 插入) / 参考字数。"""
    r, h = list(ref), list(hyp)
    n, m = len(r), len(h)
    prev = list(range(m + 1))
    # 记操作类型：0=替换 1=删除 2=插入
    sub = dele = ins = 0
    grid = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        grid[i][0] = i
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = r[i - 1] == h[j - 1]
            grid[i][j] = min(grid[i - 1][j - 1] + (0 if same else 1),
                             grid[i - 1][j] + 1,
                             grid[i][j - 1] + 1)
    # 回溯统计
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and grid[i][j] == grid[i - 1][j - 1] + (
                0 if r[i - 1] == h[j - 1] else 1):
            if r[i - 1] != h[j - 1]:
                sub += 1
            i -= 1
            j -= 1
        elif i > 0 and grid[i][j] == grid[i - 1][j] + 1:
            dele += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return (sub + dele + ins) / max(1, n), sub, dele, ins


def read_wav(path: Path) -> np.ndarray:
    """读 16k / 16bit / 单声道 PCM。转换用 ffmpeg 预先做好，脚本里不做重采样。"""
    import wave
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000, f"需要 16k: {path}"
        assert w.getsampwidth() == 2, f"需要 16bit: {path}"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(eng: AsrEngine, samples: np.ndarray, sr: int = 16000) -> str:
    st = eng.recognizer.create_stream()
    st.accept_waveform(sr, samples)
    eng.recognizer.decode_stream(st)
    return (st.result.text or "").strip()


def load_refs() -> dict[str, str]:
    out: dict[str, str] = {}
    f = REF_FILE
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, text = line.partition(" ")
        out[name] = text.strip()
    return out


def run_case(eng: AsrEngine, name: str, wav: Path, ref: str, cuts: list[float]):
    samples = read_wav(wav)
    dur = len(samples) / 16000.0
    print(f"\n=== {name}  {dur:.1f}s  参考 {len(norm(ref))} 字 ===")
    print(f"参考: {ref}")

    # 整段
    t = transcribe(eng, samples)
    e, s, d, i = cer(norm(ref), norm(t))
    print(f"  [整段]        CER {e*100:5.1f}%  (替{s} 删{d} 插{i})")
    print(f"                {t}")

    for c in cuts:
        n = max(1, int(round(dur / c)))
        step = len(samples) // n
        parts = [transcribe(eng, samples[k * step:(k + 1) * step])
                 for k in range(n)]
        joined = "".join(parts)
        e, s, d, i = cer(norm(ref), norm(joined))
        print(f"  [切 {c:>4.1f}s/{n:>2} 段] CER {e*100:5.1f}%  (替{s} 删{d} 插{i})")
        print(f"                {joined}")


def main() -> int:
    sel = sys.argv[1] if len(sys.argv) > 1 else ""
    refs = load_refs()
    eng = AsrEngine()
    print("加载模型…", flush=True)
    sec = eng.load(MODELS_DIR / CONFIG.get("asr", "model_id"),
                   int(CONFIG.get("asr", "num_threads")),
                   "", CONFIG.get("asr", "provider"))
    print(f"加载完成 {sec:.1f}s", flush=True)

    cases = [("fast1.16k.wav", [5.0, 3.0, 1.5]),
             ("raokouling.16k.wav", [5.0, 3.0, 1.5])]
    for fname, cuts in cases:
        if sel and sel not in fname:
            continue
        wav = WAV_DIR / fname
        ref = refs.get(fname.replace(".16k", ""))
        if not wav.exists() or not ref:
            print(f"跳过 {fname}")
            continue
        run_case(eng, fname, wav, ref, cuts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
