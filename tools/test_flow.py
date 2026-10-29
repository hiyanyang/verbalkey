"""端到端流程自测：用 WAV 冒充麦克风，跑完整条 录音→分句→识别→上屏 链路。

不解真实麦克风，所以能反复跑、能断言每一步的结果。
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _cfg_guard  # noqa: E402
_cfg_guard.protect()
import src.ui  # noqa: F401  先把字体引擎切到 GDI，必须赶在 QApplication 之前

import numpy as np  # noqa: E402

# ---------- 1. 用 WAV 冒充麦克风 ----------

WAV = (ROOT / "models" / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
       / "test_wavs" / "qiqiu1.wav")


def load_wav(p):
    with wave.open(str(p), "rb") as f:
        sr = f.getframerate()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    s = data.astype(np.float32) / 32768.0
    if sr != 16000:
        n = int(len(s) * 16000 / sr)
        s = np.interp(np.linspace(0, len(s) - 1, n),
                      np.arange(len(s)), s).astype(np.float32)
    return s


SAMPLES = load_wav(WAV)
BLOCK = 512


class FakeStream:
    """按真实节奏把 WAV 灌进 VAD 回调，行为和 sd.InputStream 一致。"""

    def __init__(self, samplerate=None, channels=None, dtype=None,
                 blocksize=None, device=None, callback=None, **kw):
        self.callback = callback

    def start(self):
        import threading
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        pos = 0
        while pos + BLOCK <= len(SAMPLES) and getattr(self, "_alive", True):
            chunk = SAMPLES[pos:pos + BLOCK].reshape(-1, 1)
            self.callback(chunk, BLOCK, None, None)
            pos += BLOCK
            time.sleep(BLOCK / 16000.0)   # 按真实时间推进
        self._ended = True

    def stop(self):
        self._alive = False

    def close(self):
        self._alive = False


def patch_sounddevice():
    import sounddevice as sd
    sd.InputStream = FakeStream


# ---------- 2. 跑流程 ----------

def main():
    patch_sounddevice()

    from PySide6.QtWidgets import QApplication
    qt = QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    qt.setStyle("Fusion")

    import src.main as m
    app = m.App(qt)

    log_lines = []
    app.hud.flash = lambda *a, **k: log_lines.append(("flash", a))
    # 实时出字靠的是 hud.set_text，这里记下每次调用，验证「说话时真的有字」
    seen_text = []
    _orig_set_text = app.hud.set_text
    app.hud.set_text = lambda t: (seen_text.append(t), _orig_set_text(t))[1]

    # 不真的往光标里粘贴（会打进终端窗口），只记录待上屏的文本
    pasted = []
    app._paste = lambda t: (pasted.append(t), "sent")[1]
    # 整理模型要另起进程，链路自测里先跳过
    app._start_polish = lambda text, level, direct=False: None
    # 本机配置是「按一下切换」，自测要走按住松开的那条路
    from src.config import CONFIG
    CONFIG.data["hotkey"]["mode"] = "hold"
    # 自测把整理模型跳过了，所以必须走「先上屏原文」这条，
    # 否则直出模式会等一个永远不会来的整理结果，看不到上屏
    CONFIG.data["polish"]["paste_mode"] = "raw"
    CONFIG.data["polish"]["level"] = 0

    print("等 ASR 载入…")
    for _ in range(120):
        qt.processEvents()
        if app.asr.ready:
            break
        time.sleep(0.5)
    if not app.asr.ready:
        print("FAIL: ASR 没载入")
        return 1
    print("ASR 就绪")

    app._on_press()
    print("按下 -> busy =", app.busy, " recorder =", app.recorder is not None)

    # 播完 20 秒音频
    t0 = time.time()
    while time.time() - t0 < 20:
        qt.processEvents()
        time.sleep(0.05)

    print("松开前 buffer =", app.buffer)
    app._on_release()
    print("松开 -> 已请求停止")

    t1 = time.time()
    while time.time() - t1 < 30:
        qt.processEvents()
        time.sleep(0.05)
        if not app.busy:
            break

    print("busy =", app.busy)
    print("buffer =", app.buffer)
    print("last_pasted =", repr(app.last_pasted))
    print("上屏内容 =", pasted)
    print("实时出字次数 =", len(seen_text))
    for t in seen_text[:3]:
        print("  浮窗文字:", t)
    print("HUD 调用 =", log_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
