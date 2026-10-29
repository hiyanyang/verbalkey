"""配置读写。单一 JSON 文件，改完即存。"""

from __future__ import annotations

import json
import sys
import threading
from copy import deepcopy
from pathlib import Path

def _app_dir() -> Path:
    """源码跑的时候是项目根目录；打包成 exe 后，向上找带 models 目录的那层，
    这样 config / logs / models 仍然和以前在同一个地方，不会因为打包而搬家。"""
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
        for p in [start, *start.parents]:
            if (p / "models").is_dir():
                return p
        return start
    return Path(__file__).resolve().parent.parent


APP_NAME = "Verbalkey"
APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config.json"
MODELS_DIR = APP_DIR / "models"
LOGS_DIR = APP_DIR / "logs"
DATA_DIR = APP_DIR / "data"

# Windows 虚拟键码
VK = {
    "右 Alt": 0xA5,
    "右 Ctrl": 0xA3,
    "右 Shift": 0xA1,
    "CapsLock": 0x14,
    "F5": 0x74,
    "F8": 0x77,
    "` (反引号)": 0xC0,
}
VK_NAME = {v: k for k, v in VK.items()}

# 整理结果的确认键。默认右 Ctrl —— 录音键默认右 Alt，两个键分开才不会打架
VK_CONFIRM = {
    "右 Ctrl": 0xA3,
    "右 Alt": 0xA5,
    "右 Shift": 0xA1,
    "F5": 0x74,
    "F8": 0x77,
    "Enter": 0x0D,
    "` (反引号)": 0xC0,
}
VK_CONFIRM_NAME = {v: k for k, v in VK_CONFIRM.items()}

DEFAULTS = {
    "hotkey": {
        "vk": 0xA5,
        "label": "右 Alt",
        "mode": "hold",          # hold=按住说话松开停 / toggle=按一下开始再按一下停
        "confirm_vk": 0xA3,      # 整理结果的确认键，默认右 Ctrl（不和录音键打架）
        "confirm_label": "右 Ctrl",
    },
    "audio": {
        "device": None,          # None = 系统默认输入设备
        "sample_rate": 16000,
        "gain": 1.0,
    },
    "vad": {
        # 这几个值是实测定的，设置界面不再暴露，改这里才生效。
        # 依据：同一段音频切成 1.5 秒的小段，错字率 43.8%；切成 5 秒 12.3%。
        # 识别模型是整段解码的，片段越短越没有上下文，同音字必错
        # （用户实测「识别的结果」被切成 1.3 秒一段，识别成了「事变的结果」）。
        "silence_ms": 500,       # 停顿 0.5 秒才算一句说完（322 会把词间停顿也切断）
        "min_speech_ms": 400,    # 短于此的片段丢弃（1000 会吞掉「好的」这种短句）
        "max_segment_s": 10,     # VAD 自身上限
        "max_latency_s": 6,      # 一口气不停也每 6 秒先出一次字
        # 不到这个时长的段不单独送识别，先攒着跟下一段一起解（关键参数）
        "min_asr_sec": 3.0,
        "threshold": 0.5,
    },
    "asr": {
        "model_id": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "num_threads": 6,
        # 逗号分隔，走 sherpa-onnx 热词偏置。实测有效但**必须克制**：
        # 2 个词时能把「树杈」从「柱子/车刹」纠正过来；填到 7 个，0.6B 模型
        # 会把整串词表当正文复述出来（测试音频是歌，输出里却念了一遍热词）。
        # 建议不超过 3 个，只放最常错的那个词
        "hotwords": "",
        "provider": "cpu",
    },
    "text": {
        "to_simplified": True,   # Qwen3-ASR 会输出繁体，默认转简体
        "replacements": {},      # 自定义替换：{"三十六": "36"}
    },
    "polish": {
        "enabled": True,
        "level": 1,              # 0 关 / 1 去口水词 / 2 书面化
        "model_file": "",        # GGUF 文件名，位于 models 目录
        "server_bin": "",        # llama-server.exe 路径
        "idle_unload_min": 5,    # 空闲多少分钟卸载整理模型
        "keep_alive": False,     # True = 常驻内存不卸载，省掉下次冷启动
        "auto_replace": False,   # False=整理完提示手动替换，True=自动替换
        "paste_mode": "raw",     # raw=先上屏原文 / polished=等整理完直接上屏整理版
        "port": 8080,
    },
    "paste": {
        "mode": "auto",          # auto / clipboard / message
        "restore_clipboard": True,
    },
    "general": {
        "autostart": False,
        "hud_position": "bottom",  # bottom / cursor
        "hud_font_px": 18,         # 浮窗正文字号：16 小 / 18 标准 / 20 大
    },
}


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = deepcopy(DEFAULTS)
        # 关掉后所有修改只留在内存，不写盘。测试脚本必须置 False ——
        # 它会把 level 改成 0 之类来造场景，落盘一次用户的设置就没了，
        # 而且下次测试又把这份被改过的文件当「原始配置」备份，错值会一直传下去
        self.save_enabled = True
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._merge(self.data, loaded)
            except Exception:
                pass  # 配置损坏则回退默认值

    @staticmethod
    def _merge(base: dict, new: dict) -> None:
        for k, v in new.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                Config._merge(base[k], v)
            else:
                base[k] = v

    def save(self) -> None:
        with self._lock:
            if not self.save_enabled:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get(self, section: str, key: str = None):
        with self._lock:
            if key is None:
                return self.data.get(section, {})
            return self.data.get(section, {}).get(key)

    def set(self, section: str, key: str, value) -> None:
        with self._lock:
            self.data.setdefault(section, {})[key] = value
            self.save()

    def section(self, name: str) -> dict:
        with self._lock:
            return self.data.setdefault(name, {})


CONFIG = Config()
