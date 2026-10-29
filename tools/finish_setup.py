"""下载完成后的收尾：合并分片 → 解压 → 登记模型 → 载入验证 → 试识别。

用法: python tools/finish_setup.py
"""

from __future__ import annotations

import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.asr import scan_models  # noqa: E402
from src.config import CONFIG, MODELS_DIR  # noqa: E402

TARBALL = MODELS_DIR / "qwen3-asr.tar.bz2"
EXPECTED = 878702423


def merge_parts() -> bool:
    parts = sorted(MODELS_DIR.glob("part.*"),
                   key=lambda p: int(p.name.split(".")[-1]))
    if not parts:
        return TARBALL.exists()
    total = sum(p.stat().st_size for p in parts)
    print(f"分片合计 {total/1048576:.0f} MB / {EXPECTED/1048576:.0f} MB")
    if total < EXPECTED:
        print("还没下完，等下载结束再跑本脚本。")
        return False
    with open(TARBALL, "wb") as out:
        for p in parts:
            out.write(p.read_bytes())
    for p in parts:
        p.unlink()
    print("分片已合并")
    return True


def extract() -> bool:
    if not TARBALL.exists():
        print("找不到 tar.bz2")
        return False
    print("解压中…")
    with tarfile.open(TARBALL, "r:bz2") as tf:
        tf.extractall(MODELS_DIR)
    print("解压完成")
    return True


def register() -> bool:
    models = scan_models(MODELS_DIR)
    if not models:
        print("解压后没找到合法模型目录，检查文件结构")
        return False
    for m in models:
        print(f"  发现模型: {m.name}  {m.size_bytes()/1048576:.0f} MB")
    best = models[0]
    CONFIG.set("asr", "model_id", best.dir.name)
    print(f"已登记为当前模型: {best.dir.name}")
    return True


def verify():
    from src.asr import AsrEngine

    mid = CONFIG.get("asr", "model_id")
    d = MODELS_DIR / mid
    print(f"\n载入 {d.name} …")
    eng = AsrEngine()
    t0 = time.time()
    try:
        secs = eng.load(d, int(CONFIG.get("asr", "num_threads")),
                        str(CONFIG.get("asr", "hotwords") or ""), "cpu")
    except Exception as e:
        print(f"载入失败: {e}")
        return
    print(f"载入耗时: {secs:.2f} 秒（进程总计 {time.time()-t0:.2f} 秒）")

    wavs = sorted(d.rglob("*.wav"))
    if not wavs:
        wavs = sorted(MODELS_DIR.rglob("*.wav"))
    if not wavs:
        print("没有测试音频，跳过识别验证")
        return

    import numpy as np
    import wave

    for w in wavs[:3]:
        try:
            with wave.open(str(w), "rb") as f:
                sr = f.getframerate()
                data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
            samples = data.astype(np.float32) / 32768.0
            if sr != 16000:
                n = int(len(samples) * 16000 / sr)
                samples = np.interp(
                    np.linspace(0, len(samples) - 1, n),
                    np.arange(len(samples)), samples).astype(np.float32)
            t = time.time()
            text = eng.transcribe(samples, 16000)
            dur = len(samples) / 16000
            print(f"  {w.name}  {dur:.1f}s → {time.time()-t:.2f}s "
                  f"(RTF {((time.time()-t)/dur):.2f})  识别: {text}")
        except Exception as e:
            print(f"  {w.name} 失败: {e}")


def main():
    if not merge_parts():
        return 1
    extract()
    if not register():
        return 1
    verify()
    print("\n完成。双击 start.vbs 启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
