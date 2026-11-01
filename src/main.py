"""Verbalkey 入口。托盘常驻、热键录音、识别上屏、后台整理。"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QDialog, QMenu, QSystemTrayIcon,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr import AsrEngine  # noqa: E402
from src.audio import Recorder, _trim_silence, build_vad  # noqa: E402
from src import halluc, native_log  # noqa: E402
from src.config import (  # noqa: E402
    APP_NAME, APP_VERSION, CONFIG, LOGS_DIR, MODELS_DIR, VK_CONFIRM_NAME, VK_NAME,
)
from src.hotkey import HotKey  # noqa: E402
from src import hotwords  # noqa: E402
from src.paste import get_foreground, paste_text, replace_last  # noqa: E402
from src.polish import PolishEngine, rule_polish  # noqa: E402
from src.text import process  # noqa: E402
from src.ui.brand import make_icon  # noqa: E402
from src.ui.hud import Hud  # noqa: E402
from src.ui.settings import SettingsWindow  # noqa: E402

LOGS_DIR.mkdir(parents=True, exist_ok=True)
# 轮转：日志里存着每一次上屏的全文，又不清理，长期用会一直涨。
# 4MB × 4 份封顶 16MB —— 够查最近几天，也不会把硬盘慢慢吃满。
_log_handler = RotatingFileHandler(
    LOGS_DIR / "app.log", maxBytes=4 * 1024 * 1024, backupCount=3,
    encoding="utf-8")
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("verbalkey")

# 接管 C++ 层的 stderr（sherpa-onnx / onnxruntime 的诊断）。
# 必须在识别模型加载之前装好，否则首次加载阶段的输出抓不到。
# 失败也没关系：install() 自己会退化成「不捕获」，不影响主流程。
native_log.install(LOGS_DIR)

REPLACE_WINDOW_SEC = 45

# 整段重解的长度上限（秒）。实测（不带热词、6 线程 CPU）：
#   11.4s / 20.8s / 32.2s 都能完整解出（32.2s 得到 290 字，期望 292 字）
#   43.6s 开始被截断（282 字 / 期望 433 字）
#   79.7s 直接崩成一个幻觉 token「language」，一个字都不剩
# 所以 30 秒以内一次解完；超过 30 秒不再放弃重解，改成按静音分块解
# （见 _split_long）—— 这是用户 2026-10-30 明确要求的：长口述也要终稿，
# 但 30 秒以内那条路一个字都不许动。
#
# 注意：这个上限是 prompt + 音频共用 max_total_len(1024) 换来的，
# 将来若重新给解码 prompt 塞热词，可用音频会变短，这里的值要跟着降。
_MAX_REDECODE_SEC = 30.0

# 分块重解的目标块长（秒）。取 16 秒是实测出来的，不是拍脑袋：
# 同一段 51 秒音频、同一个模型，按不同块长硬切后逐块解码，总耗时
#   8s → 24.0s   12s → 27.9s   16s → 23.9s   20s → 33.8s   28s → 43.3s
# 解码耗时随音频长度**超线性**增长（5.25s 音频 0.51×实时、29s 0.67×、
# 51s 0.94×），所以块越长越亏。16 秒这一档总耗时最低，又保住了比 VAD 段
# （3 秒）长得多的上下文。
# 切口不按固定位置切，优先落在 VAD 找出的真实停顿上（_find_pauses）：
# 切在字中间的话，那个字两边各解一次，两边都认不出来 —— 正是分段丢字的
# 老毛病。实测连续朗读（无停顿素材）切出来是「随他而 / 去追它而去」。
_CHUNK_SEC = 16.0
# 为了切在真停顿上，允许一块比目标长到这个上限。宁可多解 4 秒，也不要
# 硬切在字中间 —— 硬切的代价是一个词被两边各解一次、谁都认不出来。
# 26 秒仍远低于实测安全的 32.2 秒。
_CHUNK_HARD_SEC = 26.0
# 短于这个的"停顿"不算停顿。VAD 段的样本两端自带 padding，段与段之间
# 会留下 0.08~0.15 秒的缝，那是 padding 缝不是换气，切在那里照样切断词。
_MIN_PAUSE_SEC = 0.25
# 每块最短多少秒。有个下限是为了不让一个紧贴块首的停顿把块切得太碎。
_MIN_CHUNK_SEC = 6.0
# 找不到真停顿时，往前回看的窗口：在「块尾前 3 秒」里找能量最低的一帧。
_CHUNK_SEARCH_SEC = 3.0
# 最多分几块。14 块 ≈ 224 秒有声；再长就放弃重解、退回分段草稿
# （跟分块之前的处理一样）。不设上限的话，切块时间会无限长，
# 而用户早就走神去做别的事了。
_MAX_CHUNKS = 14


# 停顿分档（秒）。依据是讯飞／百度实时 ASR 文档里那句「基于内容与停顿智能匹配
# 标点」，以及专利 CN121747549B 给出的分档：自然停顿 0.5~1s 加逗号、
# 打断 <0.3s 不标点、结束 ≥1.5s 加句号。
#
# 但阈值不能照抄：实测构造 1.5s 静音，VAD 量出来只有 0.92s —— 它在语音段
# 两端各补了约 0.3s padding，把静音吃掉了一截。
#
# 更要紧的是**不能把阈值调低**。真实日志里量到 1.03s 的地方，用户原话是
# 「哪怕我说完了很久。他才把…」「…在那个语音。屏幕里面…」—— 那是想词时的
# 停顿，不是句子结束。停顿只是弱信号，语义边界模型判断得比它准，
# 所以只有"量到 1.5s 以上"这种明显停顿才敢下句号。
_GAP_STOP = 1.60
# 低于这个间隔就当「词中间被切断」，直接接上，什么都不加。
# 以前每个段边界至少给一个逗号，于是「语音识别加，标点」「还不如，不那么
# 积极」这种词中间的缝也被塞了逗号 —— 加得太多反而影响读。
_GAP_COMMA = 0.70
# 一直没遇到停顿时的兜底：攒够这么多字才补一个逗号，免得糊成一长串。
_RUN_MAX = 55
_SR = 16000


def _similar(a: str, b: str) -> float:
    """两段文本的相似度（0~1），用来判断重解结果是「同一句的修正」还是跑偏了。"""
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _voiced_sec(x: np.ndarray, sr: int) -> float:
    """算这段音频里「真有声音」的时长，跳过静音。

    **只用于日志，不再参与任何判断**（2026-10-30 起）。分档改看总时长：
    模型吃的是整段音频，静音一样占 max_total_len 的预算，所以「有声时长
    短、静音长」的录音照样会超限 —— 拿有声时长当门槛，反而会放过这种。
    留着它是为了在日志里区分「真说了 25 秒」和「说了 25 秒但中间想了 20 秒」，
    排查重解为什么慢时要用。
    阈值与 audio._trim_silence 一致（20ms 帧、max(0.004, 峰值×0.08)），
    这样两边对「什么算有声」的判断不会打架。
    """
    if sr <= 0 or len(x) == 0:
        return 0.0
    frame = max(1, int(sr * 0.02))
    n = len(x) // frame
    if n < 2:
        return len(x) / sr
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    peak = float(rms.max())
    if peak < 0.004:
        return 0.0
    thr = max(0.004, peak * 0.08)
    return float(np.count_nonzero(rms > thr)) * frame / sr


def _find_pauses(x: np.ndarray, sr: int) -> list[tuple[int, float]]:
    """跑一遍干净的 VAD，找出这段音频里所有真实停顿。

    返回 [(静音中点采样位置, 静音秒数)]，按位置升序，只给分块重解当切点。

    为什么特意重跑一遍、而不是复用录音时那次 VAD 的结果：**录音过程中的
    seg.start 不可信**。实测（fast1+0.7s停顿+rap1 共 61 秒素材）：
    不 reset 时 start 是 160000/304896/471296…，单调递增、就是绝对位置；
    一旦 vad.reset()，下一段的 start 立刻变成 17664、然后 6912、6400 ——
    乱序且大幅回跳。而 audio._callback 每 3 秒有声就要强切一刀并 reset，
    所以实时链路里攒出来的坐标全是错的。

    这里新建一个 VAD，一次喂完，中途不 reset —— 坐标就是干净的绝对位置
    （同一份实测里已验证单调），代价是 60 秒音频多花约 1 秒。
    找不到静音（一口气念完、没换气）就返回空表，_split_long 会退回按能量切。
    """
    vad_model = MODELS_DIR / "silero_vad.onnx"
    if not len(x) or not vad_model.exists():
        return []
    try:
        vad = build_vad(str(vad_model), sr, dict(CONFIG.get("vad") or {}))
    except Exception as e:
        log.warning("找停顿失败，改按能量切：%s", e)
        return []

    out: list[tuple[int, float]] = []
    last_end = -1

    def drain():
        nonlocal last_end
        while not vad.empty():
            seg = vad.front
            s = int(getattr(seg, "start", -1))
            if s >= 0:
                if last_end >= 0 and s > last_end:
                    gap = s - last_end
                    # 切在静音正中间：两边各留半截停顿，块边界的音频最安全
                    out.append((s - gap // 2, gap / sr))
                last_end = s + len(seg.samples)
            vad.pop()

    blk = 512
    try:
        for i in range(0, len(x) - blk + 1, blk):
            vad.accept_waveform(np.asarray(x[i:i + blk], dtype=np.float32))
            drain()
        vad.flush()
        drain()
    except Exception as e:
        log.warning("找停顿出错，改按能量切：%s", e)
        return []
    return out


def _looks_degenerate(text: str, min_repeat: int = 8) -> bool:
    """识别结果退化成「复读机」时返回 True。

    greedy_search 没有重复惩罚，遇到边界奇怪或高度重复的音频会卡在循环里
    出不来。实测（2026-10-30，连续报数素材 fast1+rap1 拼 61 秒）：分块后
    第 2 块把「大跃进」连念五十遍，509 字里 400 字是同一个词，而且这一块
    解码花了 69 秒（正常应该 7 秒）。这种结果拿去替换草稿等于毁掉整段话。

    判据：某个 2~8 字的片段**连续**重复 ≥8 遍。真实内容几乎不会这样
    （「他还有二十个儿子，二十一个儿子…」每遍都不同，不会触发）。
    """
    t = (text or "").strip()
    n = len(t)
    if n < 40:
        return False
    for L in (2, 3, 4, 5, 6, 8):
        i = 0
        while i + L * min_repeat <= n:
            unit = t[i:i + L]
            k = 1
            while t[i + k * L:i + (k + 1) * L] == unit:
                k += 1
            if k >= min_repeat:
                return True
            i += k * L if k > 1 else 1
    return False


def _split_long(x: np.ndarray, sr: int,
                pauses: list[tuple[int, float]] | None = None,
                max_sec: float = _CHUNK_SEC,
                hard_sec: float = _CHUNK_HARD_SEC,
                min_sec: float = _MIN_CHUNK_SEC,
                min_pause: float = _MIN_PAUSE_SEC,
                search_sec: float = _CHUNK_SEARCH_SEC,
                ) -> list[tuple[np.ndarray, float]]:
    """把超长音频切成若干块，返回 [(块, 切口处静音秒数), ...]。

    只在整段音频超过 _MAX_REDECODE_SEC 时才会被调用（见 _redecode）；
    30 秒以内的录音走原来那条一次解完的路，不进这里。

    **切口必须落在静音上**，两条腿走路：
      1. pauses 给了 VAD 找出来的真实停顿（_find_pauses）时，取「离目标块长
         最近、且静音 ≥min_pause」的那一个，允许比目标长一点点（到 hard_sec）。
         VAD 是按声学判的，它标出来的地方确实没人说话，切在那里最安全。
         只认 ≥0.25 秒的停顿：VAD 段的样本两端自带 padding，段间会留下
         0.08~0.15 秒的缝，那是 padding 缝不是换气，切在那里照样切断词
         （实测按「取最靠后的停顿」就会选到 0.12 秒的缝上）。
      2. 没有停顿可用（连续朗读、一口气念完），才退回按能量找：在块尾前
         search_sec 秒里挑能量最低的一帧。这一档是**有损的**，可能切在词
         中间 —— 实测连续朗读切出来是「随他而」+「去追它而去」（原句
         「随它而去」被劈开）。但总比整段超过 30 秒直接放弃重解强，
         日志里会写明用了哪种切法。

    静音秒数要一起带出去：块与块之间的标点靠它决定，跟 _join_segments
    用同一套阈值。不带的话每块结尾模型都会给个句号，拼起来满屏句号。
    """
    frame = max(1, int(sr * 0.02))
    n = len(x) // frame
    if n < 2:
        return [(x, 0.0)]
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    peak = float(rms.max())
    if peak < 0.004:
        return [(x, 0.0)]
    # 阈值与 audio._trim_silence 一致，两边对「什么算静音」的判断不会打架
    thr = max(0.004, peak * 0.08)
    max_frames = max(1, int(max_sec / 0.02))
    hard_frames = max(1, int(hard_sec / 0.02))
    min_frames = max(1, int(min_sec / 0.02))
    look = max(1, int(search_sec / 0.02))

    # 停顿点换成帧下标
    marks: list[tuple[int, float]] = []
    for pos, gap in (pauses or []):
        f = int(pos // frame)
        if 0 < f < n:
            marks.append((f, gap))
    marks.sort()

    out: list[tuple[np.ndarray, float]] = []
    start = 0
    # 剩下的还超过硬上限才需要再切一刀；不到就整段留给最后一块
    while n - start > hard_frames:
        limit = start + max_frames
        pick: tuple[int, float] | None = None
        # 1) 真停顿优先：在「目标块长 ±」范围内挑**离目标最近**的真停顿。
        # 只挑离目标近的，不挑最靠后的 —— 实测按「最靠后」会选到 0.12 秒的
        # padding 缝，那等于没切在停顿上。
        best: tuple[int, int, float] | None = None
        for f, g in marks:
            if f < start + min_frames or f > start + hard_frames:
                continue
            if g < min_pause:
                continue
            d = abs(f - limit)
            if best is None or d < best[0]:
                best = (d, f, g)
        if best is not None:
            pick = (best[1], best[2])
        if pick is None:
            # 2) 退回能量最低点
            lo = max(start + min_frames, limit - look)
            i = lo + int(np.argmin(rms[lo:limit]))
            gap_sec = 0.0
            if rms[i] <= thr:
                a = i
                while a > start and rms[a - 1] <= thr:
                    a -= 1
                b = i
                while b + 1 < n and rms[b + 1] <= thr:
                    b += 1
                gap_sec = (b - a + 1) * frame / sr
            pick = (i, gap_sec)
        out.append((x[start * frame:pick[0] * frame], pick[1]))
        start = pick[0]
    out.append((x[start * frame:], 0.0))
    return out


def _join_chunks(parts: list[tuple[str, float]]) -> str:
    """把各块的识别结果拼回一段，块间标点由切口处的静音长度决定。

    规则与 _join_segments 一致：中间块结尾模型给的标点全部丢掉（它看不到
    后文，句号没有语义依据），按停顿补一个句号或逗号；最后一块的结尾
    问号/感叹号可信，留着。

    停顿挂在**切口前那一块**身上（_split_long 的返回值就是这样配的），
    所以分隔第 i 块用的是第 i-1 块的 gap —— 不是自己那份。
    """
    clean = [(t.strip(), g or 0.0) for t, g in parts if t and t.strip()]
    if not clean:
        return ""
    out: list[str] = []
    last_i = len(clean) - 1
    for i, (t, _gap) in enumerate(clean):
        if i == last_i:
            t = t.rstrip("。．.，,、；;：:")
        else:
            t = t.rstrip("。．.！!？?，,、；;：:")
        if not t:
            continue
        if not out:
            out.append(t)
            continue
        out.append(("。" if clean[i - 1][1] >= _GAP_STOP else "，") + t)
    return "".join(out)


def _join_segments(parts: list[tuple[str, int]]) -> str:
    """把一轮录音里的多个识别段拼成一段，**按停顿长短**决定标点。

    每段是 VAD 按静音切出来的，各自独立识别、各自按「一句话」加了句号。
    ASR 层看不到后文，它给的句号没有语义依据 —— 实测原话被切成：
      「是不是该用那种。可能会比较危险的方法…用在合法合理的。领域就行」
    三个句号全断在词组中间，读起来像口吃。

    以前这里一律把段间标点换成逗号，后果是「一逗到底」：该断的地方不断，
    再由 _split_long 按字数硬切，断点跟说话人的呼吸完全无关。

    现在改用 VAD 段之间的真实静音长度（audio.py 算好的 gap）：
      停顿够长（量到 ≥0.9s）→ 说完了，句号
      停顿短              → 换气或者词中间被切断，逗号
    拿声学事实断句，比让语言模型从纯文本里猜可靠 —— 停顿是说话人自己
    给出的句子边界，模型再强也只能从字面推测。
    """
    clean: list[tuple[str, int]] = []
    for text, gap in parts:
        t = (text or "").strip()
        if t:
            clean.append((t, gap or 0))
    if not clean:
        return ""

    out: list[str] = []
    last_i = len(clean) - 1
    run = 0          # 自上次标点以来累积的字数
    for i, (t, gap) in enumerate(clean):
        if i == last_i:
            # 最后一段是用户真正说完的地方，它的问号/感叹号可信，留着
            t = t.rstrip("。．.，,、；;：:")
        else:
            # 中间段的结尾标点全部丢掉，由停顿重新决定
            t = t.rstrip("。．.！!？?，,、；;：:")
        if not t:
            continue
        if not out:
            out.append(t)
            run = len(t)
            continue
        sec = gap / _SR if gap > 0 else 0.0
        if sec >= _GAP_STOP:
            out.append("。" + t)
            run = len(t)
        elif sec >= _GAP_COMMA:
            out.append("，" + t)
            run = len(t)
        else:
            # 停顿太短，这是词中间被切断：直接接上，不加标点
            out.append(t)
            run += len(t)
            if run >= _RUN_MAX:
                out.append("，")
                run = 0
    s = "".join(out).rstrip("，、,;；:")   # 兜底逗号可能落在最末尾，别留「，。」
    if s and s[-1] not in "。！？!?":
        s += "。"
    return s


class Bridge(QObject):
    """跨线程信号。钩子线程和工作线程只 emit，UI 全在主线程更新。"""

    pressed = Signal()
    released = Signal()
    segment = Signal(str, int, bool)   # 文本, 距离上一段的静音采样数, 是否可信
    finished = Signal()
    state = Signal(str)
    hint = Signal(str)                 # 浮窗副标题，工作线程用来报进度/等多久
    polished = Signal(str, str, int)   # 文本, 错误信息, 请求序号
    error = Signal(str)
    asr_ready = Signal()               # 识别模型加载完成


class App:
    def __init__(self, qt_app: QApplication):
        self.qt = qt_app
        self.bridge = Bridge()
        self.hud = Hud()
        self.asr = AsrEngine()
        self.polish = PolishEngine()
        self.recorder = None
        self._rec_args = None            # (设备, 采样率, VAD 配置) 快照
        self.hotkey = None
        self.confirm_key = None
        self.settings = None

        self.buffer: list[tuple[str, int]] = []   # (识别文本, 前置停顿采样数)
        # 与 buffer 一一对应的「可不可信」标记。不可信的段照样进 buffer
        # （终稿重解要拿它当基准比对），只是不往浮窗上显示。
        self._trust: list[bool] = []
        # 浮窗上真正显示的那几段。不可信的段落填空串，所以它的长度总是
        # 和 buffer 一样，眼睛看到的旧内容不会被后面的段覆盖。
        self._disp: list[str] = []
        # 本轮到目前为止有没有解出过中文。用来启用「整段不是中文」这条判据：
        # 只有前面说的是中文时，突然冒出一整段英文才可疑；用户整场讲英文
        # 就不会触发。
        self._saw_cjk = False
        self.last_pasted = ""            # 最近一次真正写进光标的内容
        # (整理版, 过期时间, 上屏序号)。序号是这次整理「对应哪一次上屏」的唯一凭据
        self.pending_replace = None
        # 每成功上屏一次 +1。整理结果回来时拿它比对：对不上说明用户又说了一段，
        # 这份结果已经属于上一条文字，绝不能拿去替换新内容。
        self._paste_seq = 0
        # 「直连模式」（先不上屏、等整理完一次写入）下，结果回来时如果用户正在
        # 录下一段，不能直接丢——那段文字根本还没写进任何地方，丢了就凭空少一句。
        # 排队，等下一次写的时候按顺序补上。
        self._direct_backlog: list[str] = []
        self._last_raw = ""              # 最近一次的识别原文，整理失败时兜底
        # 整理是异步的，每次请求发一个序号。结果回来时序号对不上就丢掉，
        # 否则「连环口述」时上一次的整理结果会打到现在这个窗口里
        self._polish_seq = 0
        self._polish_ctx = None          # (序号, 是否直出, 原文)
        self.busy = False
        # 上一轮的整段重解正在跑（占着识别锁）。用户这时按热键，说的话只能
        # 排队等锁，浮窗必须如实显示「等待中」，不能显示「识别中」——
        # 那会让人以为识别坏了，或者以为还能接着说。
        self.redecoding = False
        # 本热键轮已经松开。busy 要等队列排空、_on_finish 才清，这中间
        # 光看 busy 分不清「还在按着」和「已经松手在等识别」。
        self._released = False
        self.target_hwnd = 0             # 按下热键时的前台窗口，上屏前要把焦点还给它
        # 本次录音的最大峰值，用来判断麦克风音量是不是低到影响识别
        self._last_peak = 0.0
        self._peak_warned = False

        self._wire()
        self._tray()
        self._make_recorder()        # 预热麦克风：常开流，按下零延迟
        self._load_asr_async()
        # 没模型时直接把「下什么、多大」摆出来，别让用户去翻 README 找链接。
        # 延后一拍再弹：先让托盘图标出来，用户知道程序已经启动了
        QTimer.singleShot(900, self._first_run_if_needed)
        self._start_hotkeys()
        self.apply_polish_config()

    # ---------- 装配 ----------

    def _wire(self):
        b = self.bridge
        b.pressed.connect(self._on_press)
        b.released.connect(self._on_release)
        b.segment.connect(self._on_segment_ui)
        b.finished.connect(self._on_finish)
        b.state.connect(lambda s: self.hud.set_state(s))
        b.hint.connect(lambda t: self.hud.set_hint(t))
        b.polished.connect(self._on_polished)
        b.error.connect(lambda m: (log.error(m), self.hud.flash("error", m, 3000)))
        b.asr_ready.connect(self._on_asr_ready)

    def _tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self.qt)
        self.tray.setToolTip(self._tip_text())
        menu = QMenu()
        a_setup = QAction("下载模型与组件…", menu)
        a_setup.triggered.connect(self.open_setup)
        a_settings = QAction("设置…", menu)
        a_settings.triggered.connect(self.open_settings)
        a_reload = QAction("重载识别模型", menu)
        a_reload.triggered.connect(self.reload_asr)
        a_quit = QAction("退出", menu)
        a_quit.triggered.connect(self.quit)
        menu.addAction(a_setup)
        menu.addAction(a_settings)
        menu.addAction(a_reload)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.open_settings() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _tip_text(self) -> str:
        if not self.asr.ready:
            return f"{APP_NAME}（识别模型加载中…）"
        name = VK_NAME.get(int(CONFIG.get("hotkey", "vk")), "右 Alt")
        mode = CONFIG.get("hotkey", "mode")
        act = "按一下开始，再按一下停" if mode == "toggle" else "按住说话，松开上屏"
        return f"{APP_NAME}（{name}：{act}）"

    def _on_asr_ready(self):
        """模型加载完：更新托盘文字，并告诉用户现在可以按了。

        启动时模型要读 6 秒左右，这期间按热键只会得到「还在加载」。
        与其让用户猜，不如加载完主动说一声。
        """
        try:
            self.tray.setToolTip(self._tip_text())
        except Exception:
            pass
        name = VK_NAME.get(int(CONFIG.get("hotkey", "vk")), "右 Alt")
        try:
            self.tray.showMessage(
                "识别模型已就绪",
                f"按住 {name} 说话，松开即上屏",
                QSystemTrayIcon.Information, 4000)
        except Exception:
            pass

    def _start_hotkeys(self):
        vk = int(CONFIG.get("hotkey", "vk"))
        self.hotkey = HotKey(
            vk, lambda: self.bridge.pressed.emit(),
            lambda: self.bridge.released.emit(), swallow=True,
            on_error=lambda m: self.bridge.error.emit(
                f"{m}。换个热键，或把 Verbalkey 加进安全软件白名单"))
        self.hotkey.start()
        self._start_confirm_key()

    def _start_confirm_key(self):
        """整理结果的确认键。和录音键撞了就不装，否则按一次会同时触发两件事。"""
        if self.confirm_key:
            self.confirm_key.stop()
            self.confirm_key = None
        cvk = int(CONFIG.get("hotkey", "confirm_vk") or 0xA3)
        if cvk == int(CONFIG.get("hotkey", "vk")):
            log.warning("确认键和录音键相同，已跳过确认键注册")
            return
        # 不吞键：别的程序可能用得上这个键，只在有待替换内容时才动作
        self.confirm_key = HotKey(
            cvk, self._confirm, lambda: None, swallow=False,
            on_error=lambda m: log.warning("确认键注册失败: %s", m))
        self.confirm_key.start()

    def reload_hotkey(self):
        if self.hotkey:
            self.hotkey.set_vk(int(CONFIG.get("hotkey", "vk")))
        self._start_confirm_key()
        if hasattr(self, "tray"):
            self.tray.setToolTip(self._tip_text())

    # ---------- 模型 ----------

    def _first_run_if_needed(self):
        """缺识别模型就弹引导。只在真缺的时候弹，不打扰老用户。"""
        try:
            from src import fetch as F
            # 断句模型可能被单独删掉（杀毒软件），先试着从备份恢复。
            # 恢复成功就不该再弹下载引导 —— 那不是用户需要处理的事。
            state = F.ensure_vad()
            if state == "restored":
                log.warning("启动时发现断句模型缺失，已从备份恢复")
                self.hud.flash("done", "断句模型被删了，已自动恢复", 3500)
            elif state == "missing":
                log.error("断句模型缺失且没有备份，需要重新下载")
            missing = F.check(["vad", "asr"])
        except Exception as e:
            log.warning("检查模型失败：%s", e)
            return
        if not missing:
            return
        log.info("缺少识别模型 %d 项，弹出下载引导", len(missing))
        self.open_setup()

    def open_setup(self):
        """首次引导 / 补装组件。装完自动重载识别模型。"""
        try:
            from src.ui.firstrun import FirstRunDialog
        except Exception:
            log.exception("引导窗口导入失败")
            self.hud.flash("error", "下载引导打不开，看 logs/app.log", 4000)
            return
        try:
            dlg = FirstRunDialog()
        except Exception:
            log.exception("引导窗口创建失败")
            self.hud.flash("error", "下载引导打不开，看 logs/app.log", 4000)
            return
        # 引导窗口必须压在最上层。用户双击 exe 时前台常常还是浏览器或资源管理器，
        # 窗口老老实实开在别人后面 —— 用户看到的就是「双击没反应」，然后就没有然后了。
        # README 里那条「双击没反应」的排查项，有一半是这么来的。
        try:
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            log.exception("引导窗口置前失败（不影响下载）")
        log.info("下载引导已打开")
        ok = dlg.exec() == QDialog.Accepted
        if ok:
            self.reload_asr()
            try:
                self.tray.showMessage(
                    "组件已就绪", "识别模型加载中，几秒后就能按热键说话",
                    QSystemTrayIcon.Information, 4000)
            except Exception:
                pass

    def _load_asr_async(self):
        # 连点「重载模型」会起好几个加载线程，每个都要吃 800MB 和几秒 CPU。
        # 只允许一个在跑
        if getattr(self, "_loading_asr", False):
            log.info("识别模型正在加载，跳过重复请求")
            return
        self._loading_asr = True

        def work():
            try:
                mid = CONFIG.get("asr", "model_id")
                d = MODELS_DIR / mid
                if not d.exists():
                    from src.asr import scan_models
                    found = scan_models(MODELS_DIR)
                    if not found:
                        self.bridge.error.emit(
                            f"未找到识别模型，请把模型放进 {MODELS_DIR}")
                        return
                    d = found[0].dir
                    CONFIG.set("asr", "model_id", d.name)
                t0 = time.time()
                # 热词：手动词 > 从历史输入里自动挖的词 > 内置业务词。
                # 挖词是纯统计，10ms 级，放在这个加载线程里跑不影响界面。
                from src import hotwords
                manual = str(CONFIG.get("asr", "hotwords") or "")
                hw = hotwords.for_asr(manual)
                log.info(f"热词 {len(hw.split(',')) if hw else 0} 个：{hw[:80]}")
                secs = self.asr.load(
                    d,
                    int(CONFIG.get("asr", "num_threads")),
                    hw,
                    str(CONFIG.get("asr", "provider") or "cpu"),
                )
                log.info(f"ASR 载入 {d.name}，耗时 {secs:.2f}s（{time.time()-t0:.2f}s 总计）")
                self.bridge.asr_ready.emit()
            except Exception as e:
                log.exception(e)
                self.bridge.error.emit(f"识别模型载入失败: {e}")
            finally:
                self._loading_asr = False

        threading.Thread(target=work, daemon=True, name="load-asr").start()

    def reload_asr(self):
        if getattr(self, "_loading_asr", False):
            self.hud.flash("error", "模型正在加载中", 1500)
            return
        self.asr.unload()
        self._load_asr_async()

    def apply_polish_config(self):
        # 常驻：把空闲卸载时间当 0 传进去，polish 内部就是「永不自动退出」
        idle = float(CONFIG.get("polish", "idle_unload_min"))
        if CONFIG.get("polish", "keep_alive"):
            idle = 0.0
        self.polish.configure(
            str(CONFIG.get("polish", "server_bin") or ""),
            str(CONFIG.get("polish", "model_file") or ""),
            int(CONFIG.get("polish", "port")),
            idle,
        )

    def _prewarm_polish(self):
        """按下热键就把整理模型拉起来，顺便把前缀缓存烘热。

        冷启动要 4.5 秒，如果等松键才起，用户就要干等这 4.5 秒。
        边说的时候它已经在加载（或已经就绪），松键后基本只剩推理时间。

        `warmup()` 补的是另一段：进程起来后前缀还是凉的，第一次请求要多花
        4 秒 prefill（6.12s vs 1.73s）。用户说话这几秒正好够把它烘热。
        """
        if not CONFIG.get("polish", "enabled"):
            return
        if int(CONFIG.get("polish", "level") or 0) < 2:
            return      # 去口水词走规则，根本不用起模型
        if self.polish.running:
            return

        def worker():
            ok, _ = self.polish.ensure()
            if ok:
                self.polish.warmup()

        threading.Thread(target=worker, daemon=True,
                         name="polish-prewarm").start()

    # ---------- 录音周期 ----------

    def _start(self):
        if self.busy:
            return
        if not self.asr.ready:
            self.hud.flash("error", "识别模型还在加载，稍等一下", 2500)
            return
        self.buffer = []
        self._trust = []
        self._disp = []
        self._saw_cjk = False
        self.pending_replace = None
        self._last_peak = 0.0
        # 记住按热键时焦点在哪个窗口，上屏前要把焦点还回去
        self.target_hwnd = get_foreground()
        self.busy = True
        self._released = False
        self._prewarm_polish()
        self._make_recorder()
        if not self.recorder or not self.recorder.start():
            self.busy = False
            return
        if self.redecoding:
            # 上一轮的整段重解还占着识别锁。录音照样开始（麦克风全程缓冲，
            # 声音一点不会丢），但不能显示「录音中」就完事 —— 用户会以为
            # 文字马上出来，实际要等重解跑完才轮得到这一轮。
            self.hud.show_hud("waiting", "")
            self.hud.set_hint("上一段还在重解，请稍等。你现在说的话会录下来，不会丢")
            return
        self.hud.show_hud("recording", "")

    def _stop(self):
        if not self.busy or not self.recorder:
            return
        self._released = True
        self.recorder.stop()
        if self.redecoding:
            # 别显示「识别中」：这一句还在排队，一个字都还没开始解
            self.hud.set_state("waiting")
            self.hud.set_hint("上一段还在重解，排队等它跑完再识别这一句…")
            return
        self.hud.set_state("recognizing")

    def _on_press(self):
        log.info("热键按下")
        if CONFIG.get("hotkey", "mode") == "toggle":
            # 切换模式：按一下开始，再按一下结束
            if self.busy:
                self._stop()
            else:
                self._start()
            return
        self._start()

    def _make_recorder(self):
        # 断句模型可能被杀毒软件隔离掉（只有 629 KB，行为又像「加载二进制」）。
        # 先试着自己从备份长回来，长不回来才报错 —— 让用户对着「缺少 VAD 模型:
        # D:\...\silero_vad.onnx」这种一行英文路径干瞪眼，等于没说。
        try:
            from src import fetch as F
            state = F.ensure_vad()
        except Exception as e:
            log.warning("检查断句模型失败：%s", e)
            state = "ok"
        if state == "restored":
            log.warning("断句模型之前不见了，已从备份恢复（多半被杀毒软件隔离过）")
            self.bridge.hint.emit("断句模型被删了，已自动恢复")
        elif state == "missing":
            log.error("断句模型缺失且没有备份：%s", MODELS_DIR / "silero_vad.onnx")
            self.bridge.error.emit(
                "断句模型文件不见了，没找到备份。打开设置→检查组件，重新下载即可")
            return
        vad_model = MODELS_DIR / "silero_vad.onnx"
        if not vad_model.exists():
            self.bridge.error.emit("缺少 VAD 模型: %s" % vad_model)
            return
        sr = int(CONFIG.get("audio", "sample_rate"))
        dev = CONFIG.get("audio", "device")
        vad_cfg = dict(CONFIG.get("vad") or {})
        args = (dev, sr, tuple(sorted(vad_cfg.items())))
        if self.recorder and self._rec_args == args:
            return                   # 参数没变：沿用已经打开的流，零延迟
        if self.recorder:
            self.recorder.close()
        self.recorder = Recorder(
            vad_model=str(vad_model),
            sample_rate=sr,
            vad_cfg=vad_cfg,
            device=dev,
            on_segment=self._decode_segment,
            on_error=lambda m: self.bridge.error.emit(m),
            on_finished=lambda: self.bridge.finished.emit(),
        )
        self._rec_args = args
        self.recorder.open()

    def _decode_segment(self, samples, forced: bool = False, gap: int = 0):
        """在录音线程里跑识别，阻塞但串行。

        gap 是这一段相对上一段隔了多少采样点的静音。以前这里是「forced 就把
        句号换成逗号」，但那是拿"是不是硬切"这一个布尔值去猜标点；现在把
        真实停顿长度一路带到 _join_segments，由它按分档决定。
        """
        try:
            t0 = time.time()
            # 记峰值：低于 -30dBFS 说明麦克风基本没收到声音，
            # 这种录音模型只能靠语言先验瞎猜，错字会明显变多
            if samples.size:
                peak = float(abs(samples).max())
                if peak > self._last_peak:
                    self._last_peak = peak
            # 识别锁被上一轮的整段重解占着，transcribe 会在这儿阻塞几秒甚至
            # 十几秒（分块重解）。阻塞前先说清楚：这不是在识别，是在排队。
            waited = self.redecoding
            if waited:
                self.bridge.state.emit("waiting")
                self.bridge.hint.emit("上一段还在重解，排队等它跑完再识别这一句…")
            text = self.asr.transcribe(samples, int(CONFIG.get("audio", "sample_rate")))
            if waited and not self.redecoding:
                # 锁放开了，这一句才开始解。把「等待中」换回真实状态，
                # 否则用户会以为还卡着。
                self.bridge.hint.emit("")
                self.bridge.state.emit("recognizing" if self._released else "recording")
            log.info("识别 %.1fs 音频，峰值 %.3f，耗时 %.2fs%s%s",
                     len(samples) / 16000, self._last_peak, time.time() - t0,
                     "（硬切）" if forced else "",
                     f"，前置停顿 {gap / 16000:.2f}s" if gap and gap > 0 else "")
            if text:
                # 草稿里出现「根本没说过的话」时，别往浮窗上放。判据只有两条，
                # 都是实测站得住的（见 src/halluc.py 顶部）：整段是模型的内部
                # 标记词、或者前面说中文这一段却一个汉字都没有。
                # 判成可疑也照样 emit（trusted=False），让 buffer 保留原文，
                # 好让终稿重解有得比对 —— 只影响显示，不改上屏内容。
                why = halluc.judge(text, self._saw_cjk)
                if why:
                    log.warning("草稿暂不显示（%s）：%s", why, text[:60])
                elif halluc.has_cjk(text):
                    self._saw_cjk = True
                self.bridge.segment.emit(text, gap, not why)
        except Exception as e:
            log.exception(e)
            self.bridge.error.emit(f"识别出错: {e}")

    def _on_segment_ui(self, text: str, gap: int = 0, trusted: bool = True):
        self.buffer.append((text, gap))
        self._trust.append(trusted)
        # 不可信的段在浮窗上留空：既不让它显示出来，也不打乱已显示的顺序。
        # 真话不会因此丢掉 —— 它在 buffer 里，终稿重解会补回浮窗。
        self._disp.append(text if trusted else "")
        self.hud.set_text("".join(self._disp))
        self.hud.set_state("recording")

    def _draft_for_display(self, final: str, level: int) -> str:
        """草稿的「可显示版」：跳过不可信的段，其余照正常流程处理。

        只在确实有段被判可疑时才重算一次（正则级，微秒级），平时原样返回，
        跟改动前一模一样。
        """
        if all(self._trust):
            return final
        keep = [seg for seg, ok in zip(self.buffer, self._trust) if ok]
        if not keep:
            return ""
        shown = process(_join_segments(keep), CONFIG.get("text"))
        if level == 1:
            shown = rule_polish(shown, 1)
        return shown

    def _on_release(self):
        log.info("热键松开")
        # 切换模式下松键不做事，由下一次按下结束
        if CONFIG.get("hotkey", "mode") == "toggle":
            return
        self._stop()

    # ---------- 上屏 ----------

    def _paste(self, text: str) -> str:
        result = paste_text(text,
                            mode=CONFIG.get("paste", "mode"),
                            restore=bool(CONFIG.get("paste", "restore_clipboard")),
                            hwnd=self.target_hwnd)
        log.info(f"上屏 {result}: {text}")
        # 语料改在 _polish_async 里记「成对」的（分段草稿 + 整段重解）。
        # 这里原来只记最终那一句：只有结果、没有「模型原本听成什么」，
        # 学不了纠错，也看不出错在哪。成对才有学习价值。
        if result == "sent":
            # 新的一段已经上屏：之前那段待确认的整理结果已经对不上了，
            # 留着它下次按确认键就会删错字（见 _confirm 里的校验）
            self._paste_seq += 1
            self.pending_replace = None
        if result == "clipboard":
            # 没送进输入框，文本留在剪贴板里，提示别一闪而过。
            # 用 flash 而不是 show_hud：show_hud 不带定时器，窗口会一直挂着
            self.hud.flash("done", text, 6000)
            self.hud.set_hint("没找到输入框，已放在剪贴板里，自己按 Ctrl+V")
        elif result == "failed":
            self.hud.flash("error", "上屏失败，检查一下剪贴板权限", 3000)
        return result

    def _on_finish(self):
        if self.buffer:
            # 诊断用：段数 + 每段的前置停顿。断句完全靠这个数据，
            # 标点不对时先看这行 —— 是段数不对，还是停顿没采到
            log.info("拼接 %d 段，各段前置停顿 %s 秒", len(self.buffer),
                     [round((g or 0) / _SR, 2) for _, g in self.buffer])
        raw = _join_segments(self.buffer)
        self.busy = False
        # 取走整轮音频。必须在下一轮 start() 之前取 —— 那边会清空。
        # 分段结果是「草稿」，整段重解才是「终稿」，两者差在上下文是否完整。
        utterance = self.recorder.take_utterance() if self.recorder else None
        if utterance is not None and self.recorder and self.recorder.utterance_truncated:
            # 全程缓冲有上限（Recorder.ALL_MAX_SEC）。超了就是音频不完整，
            # 拿它重解只会丢掉后半段 —— 而草稿里有全文，替换过来等于砍掉一截。
            log.warning("本轮录音超过 %.0fs，全程缓冲已截断，跳过整段重解",
                        Recorder.ALL_MAX_SEC)
            utterance = None
        if utterance is not None:
            log.info("本轮音频共 %.1fs，进入整段重解", len(utterance) / _SR)
        if not raw:
            self.hud.flash("error", "没听到内容", 1800)
            return

        # 峰值太低说明麦克风基本没拾到音，这时的错字不是模型的锅。
        # 只提醒一次，而且用托盘气泡 —— 不能打断上屏流程
        if 0 < self._last_peak < 0.03 and not self._peak_warned:
            self._peak_warned = True
            log.warning("录音峰值仅 %.3f，麦克风音量偏低", self._last_peak)
            try:
                self.tray.showMessage(
                    "麦克风音量偏低",
                    "识别会明显变不准。右键任务栏音量图标 → 声音设置 → 输入，"
                    "把音量调高，或者换一个麦克风",
                    QSystemTrayIcon.Warning, 6000)
            except Exception:
                pass

        self.hud.hide_actions()
        final = process(raw, CONFIG.get("text"))
        level = 0
        if CONFIG.get("polish", "enabled"):
            level = int(CONFIG.get("polish", "level") or 0)

        if level == 1:
            # 去口水词用本地规则，微秒级做完。直接粘清理过的文本，一次到位，
            # 不走「先上屏原文再等整理」那条路，所以这一档是真正的零等待。
            final = rule_polish(final, 1)
        self._last_raw = final
        # 浮窗上显示的那一份：跳过被判可疑的段。上屏内容一步都不改 ——
        # 走的是 shown 这个副本，final 原样交给 _paste 和重解。
        shown = self._draft_for_display(final, level)
        if not shown and final:
            # 整轮都被判可疑，浮窗空着会像「什么都没识别到」，给个占位
            shown = "…"

        want_polish = level >= 2
        # 「等整理好一次写进去」不只对书面化有意义。去口水词（level=1）这一档
        # 同样要整段重解一次来修分段边界丢的字，那 2~4 秒里先写草稿再替换，
        # 用户看到的就是文字跳变 + 光标被打断。所以只要后面还有结果要回来，
        # 就尊重这个开关，别管有没有跑大模型。
        has_later = want_polish or (level == 1 and utterance is not None)
        direct = has_later and CONFIG.get("polish", "paste_mode") == "polished"

        if direct:
            log.info("直写模式：草稿只走浮窗，等终稿再写入（级别 %d）", level)
            self.hud.show_hud("polishing", shown)
            self.hud.set_hint("整段重解 + 整理中，会直接写进光标" if want_polish
                              else "整段重解中，好了直接写进光标")
            self._start_polish(final, level, direct=True, utterance=utterance)
            return

        result = self._paste(final)
        if result == "sent":
            # 只有真送进输入框了才记为「已写入」。没送进去（比如只进了剪贴板）
            # 却记下来的话，之后按确认键会按这个长度退格，删到上一段的字上。
            self.last_pasted = final
        else:
            # 草稿没进光标，就没有「可替换的基准」。这时候再拿重解结果去
            # 退格替换，会删到别处的内容上 —— 直接放弃这一轮重解。
            utterance = None
        if result == "sent":
            if want_polish:
                # 整理实测要 1~4 秒，比 flash 的 1.6 秒长得多。用 flash 的话
                # 浮窗会先到点自动隐藏，等整理结果回来 _on_polished 又把它
                # 重新 show 出来 —— 用户看到的就是「消失一下再弹出来」。
                # 所以这里给一个远大于整理耗时的兜底时长：正常情况下整理回来
                # 时 show_hud() 会取消这个定时器；万一整理卡死，15 秒后自己收。
                self.hud.flash("done", shown, 30000)
                self.hud.set_hint("整段重解 + 整理中…")
            elif utterance is not None:
                # 没开整理、但还要整段重解。同样不能让浮窗先关掉再弹回来：
                # 重解 24 秒音频实测要 9.8 秒，1.6 秒的 flash 根本盖不住。
                # 用户实测投诉的就是这条路径（level=1 时窗口关了又冒出来）。
                self.hud.flash("done", shown, 30000)
                self.hud.set_hint("整段重解中…")
            else:
                # 这一档后面没有任何结果会回来，浮窗显示的就是真正写进去的字。
                # 所以这里给 final 而不是 shown：宁可让它跟输入框一致，
                # 也不能出现「框里有、浮窗没有」这种对不上的情况。
                self.hud.flash("done", final, 1600)
        # 没有整理时也要走一遍：整段重解能把分段边界丢的字补回来
        if want_polish or utterance is not None:
            self._start_polish(final, level, direct=False, utterance=utterance)

    def _start_polish(self, text: str, level: int, direct: bool = False,
                      paste_id: int | None = None, utterance=None):
        self._polish_seq += 1
        seq = self._polish_seq
        # paste_id 记下「这次整理对应第几次上屏」。结果回来时若上屏次数已经变了，
        # 说明用户又写了一段，这份结果必须丢掉 —— 否则会拿旧文字去替换新文字。
        # 第 5 位 redecode_only：这次只有「整段重解」、没有大模型整理。
        # 这种结果的语义是「同一句话听得更准」，不是「换了种写法」，
        # 所以不该弹窗让人按确认键 —— 直接替换掉草稿就行。
        self._polish_ctx = (seq, direct, text,
                            self._paste_seq if paste_id is None else paste_id,
                            level < 2)
        threading.Thread(target=self._polish_async,
                         args=(text, level, seq, utterance),
                         daemon=True, name="polish").start()

    def _redecode(self, utterance, draft: str) -> str:
        """拿整轮音频重解一次，返回终稿。

        为什么必须做：VAD 把语音切成段、每段单独解码，上下文在边界断掉，
        跨边界的词两半各解一次就会丢字。线上日志的直接证据：
            「拼接 5 段，各段前置停顿 [0.0,0.0,0.0,0.0,0.0]」→ 中间被硬切 4 刀
            「…长句子断句吧，个断句还是…」→ 丢了「这」
        整段重解上下文完整，这是 meya（xiaoyeqiannian/meya）等项目的标准做法：
        按住时出草稿，松开后整段重解出终稿。

        失败、超长、或用户已经在说下一段，一律退回草稿 —— 绝不让它比现在更差。

        长度分两档（用户 2026-10-30 定的口径）：
          ≤ 30 秒：一次解完，跟以前一模一样，一个字符都不动。
          > 30 秒：按停顿分块（目标 16 秒一块，为凑停顿最多 26 秒）逐块解，
                   再拼回去。以前这一档是直接放弃重解、退回分段草稿
                   （分段草稿仍会丢边界字），所以这是净增益 —— 代价是耗时
                   翻几倍，见 _decode_chunks。
        """
        sr = int(CONFIG.get("audio", "sample_rate"))
        # 整轮缓冲存的是**全程音频**：麦克风收到的每一块，不等 VAD 判断。
        # 所以里面含按键前 0.8 秒预缓冲和段间所有停顿。两头削一次：太长的
        # 静音引子会让模型顺着热词 prompt 往下编（实测有一段前面拖了 6.5 秒
        # 静音）。只削两头，中间的停顿是真停顿 —— 它也是断句的依据。
        # 尾部补齐不在这里做 —— 那是解码层的事（asr._TAIL_PAD_SEC），
        # 放这儿会被削掉。
        clean = _trim_silence(utterance, sr)
        if len(clean) < len(utterance):
            log.info("整段重解前削掉首尾静音 %.2fs → %.2fs",
                     len(utterance) / sr, len(clean) / sr)
        if not len(clean):
            return draft              # 整段都是底噪，没什么可解的
        secs = _voiced_sec(clean, sr)          # 只用于日志，判断走总时长
        total = len(clean) / max(sr, 1)
        if self.busy:
            log.info("跳过整段重解：已经在录下一段")
            return draft
        # 分档只看**总时长**。有声时长一定小于等于总时长，所以「有声 >30s」
        # 必然也「总长 >30s」，用总时长一个条件就能覆盖两边，而且它才是
        # 模型真正吃进去的长度 —— 43.6 秒那段被截断，静音一样占 token。
        chunks = None
        if total > _MAX_REDECODE_SEC:
            # 在**削过首尾静音之后**的音频上重新找停顿：坐标天然对齐，
            # 不用担心下标平移；而且这个 VAD 中途不 reset，位置可信。
            pauses = _find_pauses(clean, sr)
            chunks = _split_long(clean, sr, pauses)
            if len(chunks) > _MAX_CHUNKS:
                log.info("跳过整段重解：总长 %.1fs 要分 %d 块，超过 %d 块上限",
                         total, len(chunks), _MAX_CHUNKS)
                return draft
            log.info("整段重解改分块：有声 %.1fs / 总长 %.1fs → %d 块"
                     "（每块 ≤%.0fs，VAD 找到停顿 %d 处）",
                     secs, total, len(chunks), _CHUNK_SEC, len(pauses))
        t0 = time.time()
        # 重解期间识别锁一直被占着，用户这时按热键说的话只能排队。
        # 主线程靠这个标记把浮窗从「识别中」改成「等待中」，见 _stop/_decode_segment。
        self.redecoding = True
        try:
            if chunks is None:
                text = self.asr.transcribe(clean, sr)
            else:
                parts = self._decode_chunks(chunks, sr)
                if parts is None:
                    return draft          # 中途用户开口了，拼不全的结果不能用
                text = _join_chunks(parts)
        except Exception as e:
            log.warning("整段重解失败，沿用分段结果：%s", e)
            return draft
        finally:
            self.redecoding = False
        dt = time.time() - t0
        if not text:
            log.info("整段重解没有结果（%.2fs），沿用分段结果", dt)
            return draft
        text = process(text, CONFIG.get("text"))
        # 把「从改正里学」到的对照表应用上去。只做文本替换，不进解码 prompt ——
        # 这样不会幻觉、不占音频预算，只在原字串原样出现时才动手。
        try:
            text = hotwords.apply_corrections(text)
        except Exception as e:
            log.warning("应用纠正表失败（忽略）：%s", e)
        log.info("整段重解 %.1fs 有声（总长 %.1fs，%s），耗时 %.2fs："
                 "分段 %d 字 → 整段 %d 字",
                 secs, total, ("分 %d 块" % len(chunks)) if chunks else "整段一次",
                 dt, len(draft), len(text))
        return text

    def _decode_chunks(self, chunks: list, sr: int) -> list | None:
        """逐块解码超长录音。返回 [(文本, 切口静音秒数)]，中止时返回 None。

        两块之间的中止检查不是多余的：分块重解要占识别锁 10 秒以上，用户
        这时候多半已经开口说下一句了。继续解完剩下的块没有意义 —— 拼出来
        的是半截话，拿它去替换草稿反而更糟（见 _apply_redecode 的闸门），
        而且会一直堵着新一轮的识别。所以一旦发现新一轮已经开始，立刻收手，
        退回分段草稿。
        """
        n = len(chunks)
        parts: list[tuple[str, float]] = []
        for i, (buf, gap) in enumerate(chunks, 1):
            if self.busy:
                log.info("分块重解中止：用户已在说下一段（已解 %d/%d 块）", i - 1, n)
                self.bridge.hint.emit("")
                return None
            if not len(buf):
                continue
            if n > 1:
                # 分块时等待变长，必须让浮窗说清进度，否则用户以为卡死了
                self.bridge.hint.emit(f"整段重解中… 第 {i}/{n} 块")
            t = self.asr.transcribe(buf, sr)
            parts.append((t, gap))
            log.info("重解第 %d/%d 块：%.1fs 音频 → %d 字",
                     i, n, len(buf) / max(sr, 1), len(t or ""))
            if _looks_degenerate(t):
                # 复读结果不能要：宁可整段退回草稿，也不能把一整段话换成一串重复词
                log.warning("第 %d/%d 块解成复读（%d 字），放弃本次重解，沿用草稿",
                            i, n, len(t or ""))
                self.bridge.hint.emit("")
                return None
        if n > 1:
            self.bridge.hint.emit("")
        return parts

    def _polish_async(self, text: str, level: int, seq: int, utterance=None):
        self.bridge.state.emit("polishing")
        base = text
        if utterance is not None and len(utterance):
            base = self._redecode(utterance, text)
            # 成对记语料：左边是分段草稿，右边是整段重解。
            # 两者的差异就是「模型在边界处听错了什么」，这是挖热词和学纠错的原料。
            # 同一段音频只记一次，避免后面整理/替换再写重复行。
            try:
                hotwords.record(text, base)
            except Exception:
                pass
        if level < 2:
            # 没开整理：把整段重解的结果交回去替换草稿。
            # 一律发出去，改不改由 _apply_redecode 判断 —— 发空串会让浮窗
            # 挂着 15 秒的兜底定时器干等（谁都不来收）。
            #
            # level=1（去口水词）必须在这里再清一遍：草稿的清理在 _on_finish
            # 里，而 polished 模式下草稿只走浮窗、没上屏，终稿才是用户留下的
            # 文字。只清草稿等于这档从没生效过。
            # 只认 level==1：level=0 是「开着整理但选了不整理」，清它就是越权。
            # dedup=False：终稿里「对对对」「哈哈哈」是真语气，别压成单字。
            # 顺序要求：必须在上面 record() 之后 —— 语料存的是模型原文，
            # 清过了再记，学纠正的对照表就被规则污染了。
            out = rule_polish(base, level, dedup=False) if level == 1 else base
            self.bridge.polished.emit(out, "", seq)
            return
        t0 = time.time()
        out, err = self.polish.polish(base, level)
        # 记录耗时和来源：冷启动 / 热运行差好几倍，出问题先看这一行
        log.info(f"整理 {len(base)} 字 / L{level}，耗时 {time.time() - t0:.2f}s"
                 + (f"，降级：{err}" if err else ""))
        self.bridge.polished.emit(out, err, seq)

    def _apply_redecode(self, draft: str, text: str, pid: int) -> None:
        """整段重解出来的终稿直接替换草稿：不弹窗、不要求按键。

        三道闸，任何一道不过就保持草稿不动 —— 草稿本身是能用的，宁可少改：
          1. 上屏序号没变（中间没写过别的内容）
          2. 前台窗口还是当初那个（用户切走了就别动他的字）
          3. 两份文本足够像（说明是同一句的修正，不是重解跑偏）
        """
        if not draft or not text or text == draft:
            # 没有可替换的基准，或两份完全一样 —— 都不该动用户的字
            self.hud.hide_hud()
            return
        if pid != self._paste_seq:
            log.info("跳过自动替换：上屏序号已变（%d→%d）", pid, self._paste_seq)
            self.hud.hide_hud()
            return
        if self.target_hwnd and get_foreground() != self.target_hwnd:
            log.info("跳过自动替换：前台窗口已切换，不动用户的字")
            self.hud.hide_hud()
            return
        ratio = _similar(draft, text)
        if ratio < 0.35:
            log.info("跳过自动替换：两份文本差异过大（相似度 %.2f）", ratio)
            self.hud.hide_hud()
            return
        try:
            replace_last(draft, text,
                         mode=CONFIG.get("paste", "mode"),
                         restore=bool(CONFIG.get("paste", "restore_clipboard")),
                         hwnd=self.target_hwnd)
        except Exception as e:
            log.warning("自动替换失败（保持草稿）：%s", e)
            self.hud.hide_hud()
            return
        self.last_pasted = text
        log.info("重解结果已自动替换：%d 字 → %d 字（相似度 %.2f）",
                 len(draft), len(text), ratio)
        self._result_hud(text, 900)

    def _result_hud(self, text: str, ms: int = 0) -> bool:
        """把结果写回浮窗；**已经关掉的浮窗不再自动弹回来**。

        返回浮窗是否还可见。用户投诉的「文字已经上屏、窗口自己关了，过几秒
        又冒出来」就是结果回来时无条件 show_hud 造成的。所以这里只更新内容，
        不负责重新打开；真需要重开的场景（录下一段）由 _start 负责。
        """
        if not self.hud.isVisible():
            log.info("浮窗已关闭，结果不再弹出")
            return False
        if ms:
            self.hud.flash("done", text, ms)
        else:
            self.hud.show_hud("done", text)
        return True

    def _paste_with_backlog(self, text: str) -> None:
        """直连模式写入：先补上排队的旧结果，再写这一段，保证顺序不乱。"""
        queued, self._direct_backlog = self._direct_backlog, []
        for q in queued:
            if q:
                self._paste(q)
                self.last_pasted = q
        self._paste(text)
        self.last_pasted = text

    def _on_polished(self, text: str, err: str, seq: int = 0):
        ctx = self._polish_ctx
        if seq and ctx and seq != ctx[0]:
            log.info(f"丢弃过期的整理结果 seq={seq} 当前={ctx[0]}")
            return
        direct = ctx[1] if ctx else False
        raw = ctx[2] if ctx else self._last_raw
        # 这次整理是针对第几次上屏的。当前上屏次数已经变了，就说明这份结果
        # 属于上一条文字 —— 绝不能拿去替换新内容。
        pid = ctx[3] if ctx and len(ctx) > 3 else self._paste_seq
        # 这次只有整段重解、没跑大模型整理
        redecode_only = bool(ctx[4]) if ctx and len(ctx) > 4 else False
        if not direct and pid != self._paste_seq:
            log.info(f"丢弃过期整理结果：属于第 {pid} 次上屏，当前第 {self._paste_seq} 次")
            return
        if self.busy:
            if direct:
                # 直连模式下这段文字还没写过任何地方，丢了就是少一句。
                # 排进队列，下一次写入时按顺序补在最前面。
                self._direct_backlog.append(text or raw)
                log.info("结果排队（当前在录音），队列 %d 条", len(self._direct_backlog))
            else:
                # 草稿已经上屏了，这份结果只影响替换，丢掉不损失内容
                log.info("录音中，丢弃本次整理结果")
            return

        if err:
            # 降级的规则整理也是有结果的，别扔：它至少删掉了口水词。
            # 完全失败（text 为空）才退回原文。
            fallback = text or raw
            if direct:
                self._paste_with_backlog(fallback)
                self.hud.flash("error", f"{err}，已输入原文", 3500)
            elif text and text != raw:
                if not self._result_hud(text):
                    return          # 浮窗已经关了，不重开；草稿照样能用
                self.pending_replace = (text, time.time() + REPLACE_WINDOW_SEC, pid)
                label = VK_CONFIRM_NAME.get(
                    int(CONFIG.get("hotkey", "confirm_vk") or 0xA3), "右 Ctrl")
                self.hud.set_hint(f"{err}。按 {label} 用这份结果替换原文")
                self.hud.show_actions(self._confirm, self._ignore)
            else:
                self.hud.flash("error", f"整理未完成：{err}", 3500)
            return

        if not text:
            if direct:
                self._paste_with_backlog(raw)
                self.hud.flash("done", raw, 1800)
            return

        if direct:
            # 等结果这段时间（重解 2~4 秒）用户可能已经切到别的窗口。这时再
            # 把焦点抢回原窗口、把字塞进去，会打断他正在输的内容。宁可把字
            # 留在剪贴板：不抢焦点、不毁他现在的东西，浮窗提示一句去哪拿。
            if self.target_hwnd and get_foreground() != self.target_hwnd:
                log.info("跳过直写：前台窗口已切换，结果留在剪贴板")
                paste_text(text, mode="none",
                           restore=bool(CONFIG.get("paste", "restore_clipboard")))
                if self._result_hud(text, 3000):
                    self.hud.set_hint("已切到别的窗口，文字在剪贴板，按 Ctrl+V 粘贴")
                return
            self._paste_with_backlog(text)
            self._result_hud(text, 1100)
            return

        if redecode_only:
            # 只有整段重解：这是「同一句话听得更准」，不是换写法。
            # 之前这里走的是「弹窗 + 按右 Ctrl 替换」，结果浮窗在 1.6 秒自动
            # 关闭之后又被 show_hud() 弹回来 —— 用户实测投诉的正是这个。
            # 现在直接静默替换：不弹窗、不提示、不要求按键。
            self._apply_redecode(self.last_pasted, text, pid)
            return

        if text == self.last_pasted:
            # 整理完跟原文一模一样。以前是直接 hide，用户看到的是「浮窗
            # 莫名其妙没了」，也判断不出整理到底跑没跑。给一句话收尾更诚实。
            if self._result_hud(text, 2000):
                self.hud.set_hint("整理完了，没有需要改的地方")
            return

        if CONFIG.get("polish", "auto_replace"):
            replace_last(self.last_pasted, text,
                         mode=CONFIG.get("paste", "mode"),
                         restore=bool(CONFIG.get("paste", "restore_clipboard")),
                         hwnd=self.target_hwnd)
            self.last_pasted = text
            self.hud.flash("done", text, 1200)
        else:
            if not self._result_hud(text):
                return              # 浮窗已经关了，不重开；草稿照样能用
            self.pending_replace = (text, time.time() + REPLACE_WINDOW_SEC, pid)
            label = VK_CONFIRM_NAME.get(int(CONFIG.get("hotkey", "confirm_vk") or 0xA3),
                                        "右 Ctrl")
            self.hud.set_hint(f"按 {label}，或点下面「用整理版」，替换刚写出的原文")
            self.hud.show_actions(self._confirm, self._ignore)

    def _confirm(self):
        """用整理版替换原文。确认键和浮窗按钮都走这里。"""
        if not self.pending_replace:
            return
        if self.busy:
            # 正在录下一段：这时候退格会删到新内容上，宁可不替换
            log.info("确认替换被拒：正在录音")
            if self.hud.isVisible():
                self.hud.flash("error", "正在录音，无法替换", 1500)
            return
        text, expire, pid = self.pending_replace
        self.pending_replace = None
        if time.time() > expire:
            log.info("确认替换被拒：结果已过期")
            self.hud.hide_hud()
            return
        # 关键校验：这份整理结果必须是针对「当前这一次上屏」的。
        # 中间又说过一段的话 _paste_seq 已经 +1，旧结果一律作废 ——
        # 否则退格会删掉新段的字、再粘上旧段的整理版，用户看到的就是
        # 「把前面几段的文字替换进来了」。
        if pid != self._paste_seq:
            log.warning("确认替换被拒：结果属于第 %d 次上屏，当前第 %d 次",
                        pid, self._paste_seq)
            self.hud.flash("error", "这段整理结果对不上了，已放弃替换", 2200)
            return
        log.info("确认替换：第 %d 次上屏，%d 字 → %d 字",
                 pid, len(self.last_pasted or ""), len(text))
        replace_last(self.last_pasted, text,
                     mode=CONFIG.get("paste", "mode"),
                     restore=bool(CONFIG.get("paste", "restore_clipboard")),
                     hwnd=self.target_hwnd)
        self.last_pasted = text
        self._result_hud(text, 1200)

    def _ignore(self):
        self.pending_replace = None
        self.hud.hide_hud()

    # ---------- 其它 ----------

    def open_settings(self):
        if self.settings is None:
            self.settings = SettingsWindow(self)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def quit(self):
        # 逐项收尾，一项失败不影响其余 —— 以前整块包在一个 try 里，
        # 第一项抛异常后面的模型进程就不会被 kill，会一直挂在后台
        for name, fn in (("热键", lambda: self.hotkey and self.hotkey.stop()),
                         ("确认键", lambda: self.confirm_key and self.confirm_key.stop()),
                         ("麦克风", lambda: self.recorder and self.recorder.close()),
                         ("整理进程", self.polish.kill)):
            try:
                fn()
            except Exception as e:
                log.warning("退出时清理 %s 失败: %s", name, e)
        self.qt.quit()


def _acquire_single_instance():
    """拿一个命名互斥量。已有实例在跑就返回 None。

    双击两次会起两个进程，两个低级键盘钩子互相抢键，表现是「热键时灵时不灵」。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                      ctypes.c_wchar_p]
    # 必须先把 last error 清零再调用。CreateMutexW 成功「新建」时不会清这个
    # 值，保留的是本线程上一条 Win32 调用的错误码。2026-10-29 实测：启动
    # 路径上前面某个调用留下 183，这里就把「刚建好」误判成「已存在」，
    # 弹了个「Verbalkey 已经在运行」，进程自己把自己挡在启动前，日志一行没写。
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\VerbalkeySingleton")
    if not handle:
        return True                     # 创建失败就别拦，放行
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle                       # 必须一直持有，返回给调用方保命


def _warn_already_running() -> None:
    """提示已有实例。必须显式声明 argtypes —— 否则 ctypes 会把 Python 的
    str 当成 char* 传进去，宽字符 API 收到的是乱码指针，弹出来是乱码。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, ctypes.c_uint]
    try:
        user32.MessageBoxW(None,
                           f"{APP_NAME} 已经在运行了，看右下角托盘图标。",
                           APP_NAME, 0x40)
    except Exception:
        pass


def _start_rime_service():
    """Rime 引擎装了就顺手把它拉起来。

    小狼毫的静默安装不写开机自启，WeaselServer 不在运行的话输入法
    根本调不出来。Verbalkey 自己是开机自启的（Run 键），顺手把这件事
    也做了，用户就不用单独管 Rime。
    """
    try:
        from . import rime_setup
        if rime_setup.rime_install_dir() is None:
            return
        if rime_setup.start_service():
            log.info("已拉起 Rime 服务")
    except Exception as e:
        log.debug("拉起 Rime 服务失败: %s", e)


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _self_heal_paths() -> None:
    """程序被挪过目录后，自启项和输入法图标还指着旧 exe —— 启动时改回来。

    注册表里的路径是「注册那一刻」的快照，程序自己搬家不会通知系统。
    只在「已经有这一项、但指错了」时动手：用户主动关掉自启的，
    不会被这里重新打开。
    """
    if not getattr(sys, "frozen", False):
        return
    import winreg
    exe = Path(sys.executable).resolve()

    # 1) 开机自启
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                old, _ = winreg.QueryValueEx(k, APP_NAME)
            except FileNotFoundError:
                old = None
            want = f'"{exe}"'
            # 只在「原路径已经不存在」时才改：那是用户真的挪了目录。
            # 若原路径还在（比如又跑了一份绿色副本），改写等于让副本劫持开机自启。
            # 2026-10-30 实测踩过：把发布包解压到 D://vktest 跑了一次，
            # 自启项就被改成指向测试目录了。
            old_path = Path(str(old or "").strip('"'))
            if old and old != want and not old_path.exists():
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, want)
                log.info("自启路径已修正（原路径已失效）：%s", want)
    except OSError as e:
        log.debug("检查自启路径失败（忽略）：%s", e)

    # 2) 输入法图标（LanguageProfile 的 IconFile 存的是 exe 绝对路径）
    try:
        from . import rime_register as rr
        lang = next(iter(rr.PROFILE_LANGS))
        path = (rr.TIP_PATH + "\\" + rr.TIP_CLSID
                + r"\LanguageProfile\\" + lang + "\\" + rr.PROFILE_CLSID)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            cur, _ = winreg.QueryValueEx(k, "IconFile")
        # 用 normcase+abspath 比，别拿 Path 直接比：注册表里的写法可能带引号、
        # 大小写不同、或含 .. ，直接比会误判成「路径变了」，每次启动都重启服务
        import os
        same = (cur and os.path.normcase(os.path.abspath(cur))
                == os.path.normcase(os.path.abspath(str(exe))))
        if cur and not same:
            if Path(cur).exists():
                # 另一个副本还在用：别抢。否则便携版会改掉正式安装的图标路径
                log.debug("输入法图标指向另一个有效副本，保持不动：%s", cur)
            else:
                from . import rime_setup as rs
                ok, msg = rs.rebrand()
                log.info("输入法图标路径已修正：%s（%s）", exe, msg)
    except OSError as e:
        log.debug("检查图标路径失败（忽略）：%s", e)


def _boot_rime() -> None:
    """开机那一轮：先把指错的路径纠回来，再拉起服务。"""
    _self_heal_paths()
    # 候选窗样式写在 build/weasel.yaml 里，而那个文件只有小狼毫的 GUI 部署器
    # 才会重建（用户在托盘点一次「重新部署」就会变），它偏偏又是 WeaselServer
    # 真正读的那份 —— 一旦和设置漂移，界面不报任何错，只是字号/配色悄悄变了。
    # 2026-11-01 实测过一次：设置是 24，文件里却是 20。
    # 这里在拉起服务之前对一遍，不一致就重写，服务起来读到的就是对的，
    # 不用额外重启（重启会让正在打的字丢掉）。
    from . import rime_setup
    try:
        fixed = rime_setup.ensure_weasel_style()
    except Exception as e:          # 样式自检不该拖垮启动
        log.warning("候选窗样式自检失败（忽略）：%s", e)
        fixed = ""
    if fixed:
        log.info("候选窗样式自愈：%s", fixed)
    _start_rime_service()


def main():
    # 单实例：必须在创建 QApplication 之前判断，否则第二个实例
    # 会先把托盘图标和键盘钩子都装上，两个钩子互相抢键
    mutex = _acquire_single_instance()
    if mutex is None:
        # 写一行再弹框：万一又是误判，日志里能看见，不用靠猜
        log.warning("检测到已有实例，本次启动退出")
        _warn_already_running()
        return

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qt = QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)
    qt.setStyle("Fusion")
    qt.setApplicationName(APP_NAME)
    qt.setWindowIcon(make_icon())

    threading.Thread(target=_boot_rime, daemon=True,
                     name="rime-service").start()

    app = App(qt)
    # 启动标记：每次部署完先看这一行，确认跑的是哪一版、哪个行为开关生效。
    # 没有这行就只能靠猜（曾经把「应该已经生效」当成结论报给用户）。
    log.info("启动标记 BUILD=2026-11-01-r18 版本=%s 整段重解上限=%.0fs "
             "分块上限=%.0fs×%d 粘贴模式=%s 整理级别=%s 自动替换=%s",
             APP_VERSION,
             _MAX_REDECODE_SEC, _CHUNK_SEC, _MAX_CHUNKS,
             CONFIG.get("polish", "paste_mode"),
             CONFIG.get("polish", "level"),
             CONFIG.get("polish", "auto_replace"))
    name = VK_NAME.get(int(CONFIG.get("hotkey", "vk")), "右 Alt")
    app.tray.showMessage(
        f"{APP_NAME} 已启动",
        f"正在加载识别模型（约 6 秒），就绪后按住 {name} 说话",
        QSystemTrayIcon.Information, 3000)
    sys.exit(qt.exec())


if __name__ == "__main__":
    main()
