"""麦克风采集 + Silero VAD 分句。分句完全交给 sherpa-onnx，不自己造轮子。"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np
import sherpa_onnx


def build_vad(model_path: str, sample_rate: int, cfg: dict):
    silero = sherpa_onnx.SileroVadModelConfig(
        model=str(model_path),
        threshold=cfg.get("threshold", 0.5),
        min_silence_duration=cfg.get("silence_ms", 400) / 1000.0,
        min_speech_duration=cfg.get("min_speech_ms", 300) / 1000.0,
        window_size=512,
        max_speech_duration=cfg.get("max_segment_s", 25),
    )
    vad_cfg = sherpa_onnx.VadModelConfig(
        silero_vad=silero,
        sample_rate=sample_rate,
        num_threads=1,
        provider="cpu",
    )
    return sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)


def _edge_silence(x: np.ndarray, sample_rate: int,
                  frame_sec: float = 0.02) -> tuple[int, int]:
    """量一段音频首尾各有多少静音，返回 (头, 尾) 的采样数。

    口径与 _trim_silence 完全一致（20ms 帧、阈值 max(0.004, 峰值×0.08)），
    这样「哪算有声」的判断全局只有一套。

    为什么需要它：silero 判一句说完，靠的是「已经静了 silence_ms(500ms)」，
    这 500ms 静音**被算进了段里**。实测（2026-10-30，ar1 截 3 秒）段的
    有声区间只有 0.18~3.02 秒，段本身是 0.14~3.66 —— 尾部挂着 0.64 秒
    静音 padding。

    如果直接拿段边界算「上一段到这一段的间隔」，0.64 秒就被吃掉了：
    素材里真实 2.0 秒的停顿，按段边界只有 1.35 秒，跨过 _GAP_STOP(1.6)
    的判据就翻不过来 —— 该断句号的地方断成逗号。扣掉两端 padding 之后
    是 1.99 秒，跟真值对得上。
    """
    if len(x) == 0:
        return 0, 0
    frame = max(1, int(sample_rate * frame_sec))
    n = len(x) // frame
    if n < 2:
        return 0, 0
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    peak = float(rms.max())
    if peak < 0.004:
        return len(x), 0                      # 整段都是底噪
    idx = np.nonzero(rms > max(0.004, peak * 0.08))[0]
    if len(idx) == 0:
        return len(x), 0
    lead = int(idx[0] * frame)
    tail = int(n * frame - (idx[-1] + 1) * frame)
    return max(0, lead), max(0, tail)


def _trim_silence(x: np.ndarray, sample_rate: int,
                  frame_sec: float = 0.02, keep_sec: float = 0.20) -> np.ndarray:
    """削掉首尾的静音。

    VAD 的段两端本来就带一段静音，强制切分（一口气说太久、必须先出字）时更
    严重——实测有一段落前面拖了 6.5 秒静音。LLM 类 ASR 在这种「几乎没声音」
    的音频上不会老实交白卷，而是顺着解码 prompt 往下接，于是把热词表整段念
    出来，看起来就是凭空多了一串词。

    把静音削掉，这类音频段就构不成一句可解码的话，从根上少一半幻觉。
    """
    if len(x) == 0:
        return x
    frame = max(1, int(sample_rate * frame_sec))
    n = len(x) // frame
    if n < 2:
        return x
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    peak = float(rms.max())
    if peak < 0.004:
        return x[:0]                          # 整段都是底噪，没人在说话
    thr = max(0.004, peak * 0.08)
    idx = np.nonzero(rms > thr)[0]
    if len(idx) == 0:
        return x[:0]
    keep = int(sample_rate * keep_sec)
    start = max(0, idx[0] * frame - keep)
    end = min(len(x), (idx[-1] + 1) * frame + keep)
    return x[start:end]


class Recorder:
    """麦克风采集 + VAD 分句。

    关于「开头和结尾丢字」，这里有两个实测出来的坑：

    1. **按下才开麦，开头必丢 130~150ms**（实测 start() 到第一块音频
       132~155ms）。正常语速下差不多是一个字。解法：麦克风常开，
       非录音期间只在内存里滚一个 400ms 的预缓冲；按下时把它倒进 VAD，
       按键前的声音就补回来了。
    2. **原来 stop() 先把 _running 置 False，再停流，而 stop() 本身阻塞
       333ms**。取数据的线程一看到标志就退出，这 333ms 收到的语音和
       flush 出来的尾巴全没人处理。解法：松键后先继续收 220ms 尾巴，
       再 flush，最后才允许线程退出。

    两条出字路径：
      1. VAD 端点检测 —— 静音够久就出一句，质量最好
      2. 强制切分 —— 一口气说太久也出字，保证「边说边出字」不卡住
    """

    SILENCE_RMS = 0.008   # 低于这个能量算静音，只用来计时，不用来切句
    PREROLL_SEC = 0.80    # 按键前的预缓冲（0.4 秒救不回「先开口再按键」的半个词）
    TAIL_SEC = 0.50       # 松键后继续收多久：尾音拖得比想象中长，0.35 仍会吃掉半个字
    MIN_TAIL_SEC = 0.30   # 尾音收尾的基准时长，_flush_pending 取它的 1/5 来挡碎片
    ALL_MAX_SEC = 300     # 全程缓冲上限（秒）。分块重解最多 14 块 ×16s ≈ 224s，
                          # 300 秒够用；再长就只是防异常占内存

    def __init__(self, vad_model: str, sample_rate: int, vad_cfg: dict,
                 device=None, on_segment: Callable[[np.ndarray], None] = None,
                 on_error: Callable[[str], None] = None,
                 on_finished: Callable[[], None] = None):
        self.sample_rate = sample_rate
        self.vad_cfg = vad_cfg
        self.device = device
        self.on_segment = on_segment
        self.on_error = on_error
        self.on_finished = on_finished
        self.vad = build_vad(vad_model, sample_rate, vad_cfg)
        self._segments: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stream = None
        self._worker = None
        self._lock = threading.Lock()

        # 状态机：_armed = 用户按着；_accepting = 还在收（含松键后的尾巴）；
        # _closing = 取数据的线程可以退出了
        self._armed = False
        self._accepting = False
        self._closing = False
        self._stop_timer: threading.Timer | None = None

        # 预缓冲：非录音期间滚动的最近 400ms
        n_blocks = max(1, int(self.PREROLL_SEC * sample_rate / 512) + 2)
        from collections import deque
        self._preroll: deque = deque(maxlen=n_blocks)

        # 强制切分用：自上次出字以来攒下的音频 + 其中有声部分的时长
        self.max_latency_samples = int(
            float(vad_cfg.get("max_latency_s", 7)) * sample_rate)
        self._acc: list[np.ndarray] = []
        self._voiced = 0
        self._acc_base = 0      # _acc 第一块在整条流里的绝对起点（_reset_acc 维护）

        # 短段合并：不到 min_asr_sec 的段先攒着，跟下一段拼起来再送识别。
        # 识别模型是整段解码的，看不到别的上下文，片段越短同音字越容易错。
        self.min_asr_samples = int(
            float(vad_cfg.get("min_asr_sec", 3.0)) * sample_rate)
        self.max_segment_samples = int(
            float(vad_cfg.get("max_segment_s", 20)) * sample_rate)
        self.min_tail_samples = int(self.MIN_TAIL_SEC * sample_rate)
        # 碰到这么长的停顿、且已经攒够 min_asr_sec 时，攒着的段先单独送出去。
        # 0.70 秒与 main._GAP_COMMA 是同一个值（那里按它断逗号），两边必须
        # 同步：这里不断开，那处停顿就到不了 _join_segments，判据等于白设。
        #
        # 「还得攒够 min_asr_sec」这个附加条件是为了识别准确率：短句一断开
        # 就得单独送识别，没有上下文，同音字错得厉害（实测修前 21.9% / 43.7%，
        # 修后 0.7% / 1.9%）。用户 2026-10-30 定调「识别准是根本，断句其次」，
        # 所以短句宁可不在这里断开，继续合并让模型自己在段内按静音断句。
        # 代价：短句之间的停顿到不了 _join_segments，那里只剩「直接接上」，
        # 标点全靠模型给（它听得到静音，实测标点没少）。
        self._split_gap_samples = int(0.70 * sample_rate)
        self._pending: list[tuple[np.ndarray, int]] = []   # (音频, 距离上一段的静音采样数)
        # 上一段语音在这个**绝对坐标系**里的结束位置（采样点）。
        # 绝对坐标 = _vad_base + seg.start，原因见下面 _fed 的注释。
        self._last_end = 0
        # VAD 的坐标基准。seg.start 是「自上次 vad.reset() 以来」的位置，
        # **不是**整条流的绝对位置：实测 reset 之后立刻从 2816 这种小值重新
        # 开始（同一段素材不 reset 时是 34816 附近，加回 32000 的基准正好吻合）。
        # 而 _callback 每 3 秒有声就强切一次并 reset —— 所以录音过程中拿到的
        # seg.start 必须先加基准才能和 _last_end 比。
        #
        # 不加基准的后果是实测过的（2026-10-30，真实 Recorder 驱动 1.0s / 2.5s
        # 停顿的素材）：两处停顿全被算成 0.00s，于是 _join_segments 一律
        # 「直接接上」，再靠字数兜底加逗号 —— 就是用户说的「一逗到底」。
        self._fed = 0           # 本轮累计喂给 VAD 的采样数（含按键前预缓冲）
        self._vad_base = 0      # VAD 的 0 点对应的绝对位置

        # 整轮录音的**全程音频**：麦克风收到的每一块都进来，不等 VAD 判断。
        # 松键后拿它整段重解一次。
        #
        # 为什么必须收全程、而不是「VAD 判过有声音的段」：
        # VAD 的 min_speech_ms 是 400ms，短于它的语音段会被**丢弃**（silero
        # 官方语义：「短于此值的语音段丢弃」，官方默认 250、短句建议 100~250）。
        # 中文一个字的时长约 150~250ms，所以一个被两侧停顿夹住的单字会被判成
        # 噪声扔掉。这种字既不在分段队列（草稿）里、也不在整段缓冲里 —— 线上
        # 实测「识别锁被[占]十秒以上」两次独立识别都少同一个字，就是这个。
        #
        # 收全程之后，VAD 丢掉的字仍然在重解用的音频里，能救回来。
        # 分段草稿仍走 VAD 段（那是对的：草稿要快，不能整段解）。
        self._all_buf: list[np.ndarray] = []
        self._all_len = 0
        self._all_max = int(self.ALL_MAX_SEC * sample_rate)
        self._all_truncated = False
        # 注意：这里**不**记录 VAD 的停顿坐标。seg.start 在 vad.reset() 之后
        # 会跳回很小的值（实测 160000 → 17664 → 6912 → 6400，非单调），
        # 而 _callback 每 3 秒有声就要 reset 一次，所以录音过程中拿到的
        # 位置一律不可信。分块重解需要切口坐标，改成事后再跑一遍干净的 VAD
        # （见 main._find_pauses）。
        self._utt_lock = threading.Lock()

    def _emit_error(self, msg: str):
        if self.on_error:
            self.on_error(msg)

    def _reset_acc(self, keep_voiced: bool = False):
        """清空累积缓冲。

        keep_voiced 用于「VAD 切出一段」的情形：音频已经被 VAD 拿走了，
        必须清 _acc 免得重复，但**有声时长不能清** —— 清了的话，用户每
        停顿一次强制切分的计时就归零，一口气说很久也永远不触发。

        顺手记下新缓冲在整条流里的起点（_acc_base）：算硬切段的「前置停顿」
        时必须知道这段音频从哪儿开始，不能只看它自己的头部静音。
        """
        self._acc = []
        self._acc_base = self._fed
        if not keep_voiced:
            self._voiced = 0

    def _push(self, samples: np.ndarray, forced: bool = False,
              gap: int = 0) -> bool:
        """把一段音频交给识别线程；太短的先攒着。

        gap 是这一段和上一段之间隔了多少采样点的静音 —— 它是「这里到底停
        了多久」的唯一客观依据。口述的句子边界本来就由停顿决定，让识别层
        把这个事实传下去，比事后让语言模型从纯文本里猜准得多。

        返回**是否真的送出去识别了**。调用方用它决定要不要重置
        「累计有声时长」：段被攒着时不能重置，否则强制切分的计时永远
        归零，一口气说再久也不会先出字。

        forced=True 表示这段是硬切出来的（不是自然句尾），调用方据此
        知道「后面还有下文」，不会在中间加句号。

        **碰到停顿先断开，不并进去。** 合并会把中间的停顿一起吞掉：
        实测（2026-10-30）1.08 秒那段带着 0.08 秒间隔、2.46 秒那段带着
        2.50 秒间隔，并成一段之后只剩第一段的 0.08 秒 —— 那处明显的换气
        就被吃掉了。断开之后每段各带各的 gap 走，_join_segments 才拿得到
        真实间隔。

        **但只有攒够了（≥ min_asr_sec）才允许断开。** 断开就意味着前面那段
        要单独送识别，而段越短、模型能看到的上下文越少，同音字越容易错。
        实测（tools/probe_short_cer.py，真实语音切 1.5 秒小片、片间插 0.9 秒
        停顿，以「整段一次解」为参照算 CER）：
          · 每片单独送（段均 1.6 秒）→  21.9% / 43.7%
          · 攒到 3 秒再送（段均 3.5 秒）→  0.7% / 1.9%
        而且修后段数与「永不断开」完全一致，标点也没少 —— 不丢断句。
        所以短句即使后面有明显停顿也不断开，继续往后合并，让模型自己在段内
        按静音断句（它是听得到静音的）。
        代价：短句不再因为停顿而"提前出字"，要攒够 3 秒才送（硬切
        max_latency_s 仍是 3 秒，延迟封顶没变）。
        """
        if self._pending and gap >= self._split_gap_samples:
            pending_len = sum(len(p[0]) for p in self._pending)
            if pending_len >= self.min_asr_samples:
                self._emit_pending()        # 够长了，就在这里断开
        self._pending.append((np.asarray(samples, dtype=np.float32), gap))
        total = sum(len(p[0]) for p in self._pending)

        if total < self.min_asr_samples and not forced:
            return False                    # 太短，先攒着等下一段
        if total >= self.max_segment_samples:
            forced = True                   # 攒过头了，先送出去
        return self._emit_pending(forced=forced)

    def _emit_pending(self, forced: bool = False,
                      min_samples: int | None = None,
                      trim: bool = True) -> bool:
        """把攒着的段合并成一次识别送出去。

        合并时 gap 取第一段的：它才是这段相对上一段的真实间隔，后面几段的
        间隔属于段内部，已经被并进同一段音频里了 —— 也正因为如此，_push
        才会在句号级停顿处先断开，免得把停顿并没了。

        trim=False 时原样送出（松键收尾那条路用，它最怕削掉尾音）。
        """
        if not self._pending:
            return False
        raw = (self._pending[0][0] if len(self._pending) == 1
               else np.concatenate([p[0] for p in self._pending]))
        first_gap = self._pending[0][1]
        self._pending = []
        if trim:
            # 削掉首尾静音再送「分段识别」。硬切片段常带着几秒静音，不削的话
            # 模型会拿它当引子往下编（日志里表现为把热词表念出来）
            #
            # 但整段缓冲收的是**削之前**的原始音频：重解要的是真实录音，
            # 削首尾会把句尾那 300ms 静音一起剪掉，末字就没了（见 _all_buf 注释）。
            buf = _trim_silence(raw, self.sample_rate)
        else:
            buf = raw
        floor = (int(0.20 * self.sample_rate) if min_samples is None
                 else min_samples)
        if len(buf) < floor:
            return False                # 削完不剩什么，说明这段基本是静音
        # 先攒进整轮缓冲，再入队：入队后取数线程可能立刻解完并通知收尾，
        # 那时候再补就漏了最后一段。
        self._segments.put((buf, forced, first_gap))
        return True

    def _append_all(self, samples: np.ndarray) -> None:
        """把麦克风收到的一块音频记进全程缓冲。

        必须 copy：`samples` 是 sounddevice 回调里 indata 的视图，
        底层缓冲区会被下一次回调覆盖，不拷贝就只剩最后一抖的声音。
        `_acc` 那边同样是 copy 的，原因一样。
        """
        if samples.size == 0:
            return
        with self._utt_lock:
            if self._all_len >= self._all_max:
                self._all_truncated = True
                return                      # 异常长的录音，不再往里堆
            self._all_buf.append(samples.copy())
            self._all_len += len(samples)

    @property
    def utterance_truncated(self) -> bool:
        """这一轮的全程音频是否被 ALL_MAX_SEC 截断过。

        截断意味着 take_utterance() 拿到的不是完整录音：拿它重解只会丢掉
        后半段，而分段草稿里有全文 —— 替换过去等于砍掉一截。调用方（main）
        见到 True 就跳过整段重解。
        """
        return self._all_truncated

    def take_utterance(self) -> np.ndarray | None:
        """取走这一轮录音的全程音频，取完即清空。

        收的是麦克风收到的每一块（见 _all_buf 注释），不是 VAD 段 ——
        VAD 丢弃的短字也在这里。只有在 `_accepting` 已置 False 之后调用
        才拿得全：那之后再不会有新块进来。
        """
        with self._utt_lock:
            parts, self._all_buf = self._all_buf, []
            self._all_len = 0
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _flush_pending(self):
        """松键时把攒着的零头也送出去。

        这里原先按 300ms 设了一道门槛：不到 300ms 的零头直接扔。结果就是
        「一句话末尾丢字」—— 0.3 秒中文能说一到两个字，用户说完最后一个字
        立刻松键，那半句就被当成「不值得识别的零头」扔掉了。
        _pending 里攒的都是 VAD 判过有声音的片段，不是静音，再短也该送。

        这里**不削首尾静音**（trim=False）：松键这一口气最怕削掉尾音，而
        攒着的段本就已经是 VAD 判过有声的。削静音是给硬切段用的，两回事。
        """
        # 只挡真正的碎片：不到 60ms 的音发不出完整的字，送进去只会出乱码
        self._emit_pending(forced=False,
                           min_samples=self.min_tail_samples // 5,
                           trim=False)

    def _callback(self, indata, frames, time_info, status):
        samples = np.asarray(indata[:, 0], dtype=np.float32)
        if samples.size == 0:
            # 停流瞬间 sounddevice 可能给 0 帧，交给 sherpa-onnx 会报
            # "NULL input supplied"
            return

        if not self._accepting:
            # 没在录音：只滚预缓冲，别的什么都不做
            self._preroll.append(samples)
            return

        # 和 _finish_stop 抢同一把锁：那边正在 vad.flush() 时，这边的
        # vad.accept_waveform 会把 VAD 内部指针搞空，报 "NULL input supplied"
        with self._lock:
            if not self._accepting:
                return
            # 每一块都进全程缓冲：VAD 会按 min_speech_ms(400ms) 丢弃短语音段，
            # 被丢的字在草稿和重解里都会消失（线上实测丢过「占」）。
            # 收全程才能让重解拿到真实录音。
            self._append_all(samples)
            # 先记账再喂：喂完 VAD 就可能吐出段来，那时要用到当前绝对位置
            self._fed += len(samples)
            try:
                self.vad.accept_waveform(samples)
                # 路径 1：VAD 判定的句子结尾
                while not self.vad.empty():
                    seg = self.vad.front
                    seg_start = int(getattr(seg, "start", -1))
                    smp = np.array(seg.samples, dtype=np.float32)
                    gap = -1
                    if seg_start >= 0:
                        # seg.start 是「自上次 reset 以来」的位置，加基准才是整条
                        # 流的绝对位置（reset 会归零，见 __init__ 里 _fed 注释）。
                        #
                        # 而且段边界不是语音边界：段里挂着 VAD 判句用的 500ms
                        # 静音 padding（实测段尾 0.64s）。不扣掉的话，真实 2.0s
                        # 的停顿只量出 1.35s，跨不过 _GAP_STOP(1.6) —— 该断句号
                        # 的地方会断成逗号，正是「一逗到底」。
                        lead, tail = _edge_silence(smp, self.sample_rate)
                        abs_start = self._vad_base + seg_start + lead
                        if self._last_end > 0:
                            gap = max(0, abs_start - self._last_end)
                        self._last_end = self._vad_base + seg_start + len(smp) - tail
                    sent = self._push(smp, gap=gap)
                    self.vad.pop()
                    # 音频已被 VAD 取走，_acc 必须清（不清硬切时会重复）。
                    # _voiced 只在真的送出去识别后才清零：段被攒着时清掉的话，
                    # 强制切分的计时永远到不了，一口气说再久也不会先出字。
                    self._reset_acc(keep_voiced=not sent)

                # 路径 2：一口气说太久，强制切一刀
                self._acc.append(samples.copy())
                if float(np.sqrt(np.mean(samples ** 2))) > self.SILENCE_RMS:
                    self._voiced += len(samples)
                if self._voiced >= self.max_latency_samples and self._acc:
                    # 硬切以前一律记 gap=0（"中间没停顿，直接接上"）。但 _acc 是
                    # 「自上次被 VAD 取走以来的所有块」，里面**含中间那些静音**：
                    # 实测（2026-10-30）一次硬切段 2.98 秒，头部挂着 1.90 秒静音
                    # —— 那是上一句说完后真正停顿的两秒，被记成 0 就白丢了。
                    #
                    # 而且不能只拿「这段自己的头部静音」当 gap：VAD 判一句说完
                    # 要等 silence_ms(500ms)，那半秒音频进的是 VAD、不是 _acc，
                    # 两头都不算就少了半秒（实测把 1.0 秒停顿量成 0.40 秒）。
                    # 所以要用「这段在整条流里的起点」减「上一段语音的终点」
                    # （_acc_base 在 _reset_acc 里记下的就是这个起点）。
                    buf = np.concatenate(self._acc)
                    lead, tail = _edge_silence(buf, self.sample_rate)
                    acc_start = self._acc_base + lead
                    if self._last_end > 0:
                        gap = max(0, acc_start - self._last_end)
                    else:
                        gap = lead
                    self._push(buf, forced=True, gap=gap)
                    self._last_end = self._acc_base + len(buf) - tail
                    self._reset_acc()
                    # VAD 内部还攒着同一段，不 reset 会重复出一遍
                    self.vad.reset()
                    self._vad_base = self._fed      # reset 后 VAD 从 0 重新计数
                elif len(self._acc) > self.max_latency_samples * 3 // 512 + 60:
                    # 一直在静音，攒着的没必要留
                    self._reset_acc()
            except Exception as e:
                self._emit_error(f"VAD 出错: {e}")

    def _drain(self):
        # 松键后队列里可能还有尾巴，要处理干净再结束
        while True:
            try:
                samples, forced, gap = self._segments.get(timeout=0.2)
            except queue.Empty:
                if self._closing:
                    break
                continue
            if self.on_segment:
                try:
                    self.on_segment(samples, forced, gap)
                except Exception as e:
                    self._emit_error(f"处理音频段出错: {e}")
        if self.on_finished:
            try:
                self.on_finished()
            except Exception:
                pass

    # ---------- 生命周期 ----------

    def open(self) -> bool:
        """打开麦克风并常驻。打开失败返回 False。

        常驻是为了消掉按下热键时的 130~150ms 启动延迟。
        音频只在按住热键期间被处理，其余时间只在内存里滚 400ms 预缓冲。
        """
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                return True
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=512,
                    device=self.device,
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as e:
                self._stream = None
                self._emit_error(f"打开麦克风失败: {e}")
                return False
            return True

    def close(self) -> None:
        with self._lock:
            self._stop_timer and self._stop_timer.cancel()
            self._stop_timer = None
            self._armed = False
            self._accepting = False
            self._closing = True
            self._preroll.clear()
            stream = self._stream
            self._stream = None

        # 停流要放在锁外面：stream.stop() 会等音频回调线程退出，
        # 而回调本身要拿这把锁 —— 在锁内调用就是互相等死
        try:
            if stream:
                stream.stop()
                stream.close()
        except Exception:
            pass

    # ---------- 每一轮录音 ----------

    def start(self) -> bool:
        if not self.open():
            return False
        with self._lock:
            if self._worker and self._worker.is_alive():
                return True          # 已经在录了，别开第二个取数线程
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            self._closing = False
            # 坐标基准先归零，再清累积缓冲 —— _reset_acc 会把 _acc_base 记成
            # 当前的 _fed，顺序反了就会把上一轮的计数带进来。
            # （不能指望上一轮一定走完了 _finish_stop，异常中断时走不到。）
            self._fed = 0
            self._vad_base = 0
            # 新一轮从零开始算静音间隔。中途 vad.reset() 不清，那之后是
            # 「一口气继续往下说」，本来就该按无停顿处理
            self._last_end = 0
            self._reset_acc()
            self._pending = []
            # 新一轮：清掉上一轮留下的全程音频，否则重解会把两段话拼在一起
            with self._utt_lock:
                self._all_buf = []
                self._all_len = 0
                self._all_truncated = False
            # 把按键前的预缓冲倒进 VAD：这 400ms 就是以前丢掉的开头
            pre = list(self._preroll)
            self._preroll.clear()
            self._armed = True
            self._accepting = True
            if pre:
                # 预缓冲是按键前的真实声音，属于这一轮：既要进 VAD，
                # 也要进全程缓冲（VAD 可能判它太短而丢掉，重解得有）
                self._acc.extend(pre)
                try:
                    for blk in pre:
                        self._append_all(blk)
                        self._fed += len(blk)      # 预缓冲也是 VAD 的输入
                        self.vad.accept_waveform(blk)
                except Exception:
                    pass
            self._worker = threading.Thread(target=self._drain, daemon=True,
                                            name="asr-feed")
            self._worker.start()
            return True

    def stop(self) -> None:
        """松键。不立刻收尾：再收 220ms 尾巴，然后才 flush。"""
        with self._lock:
            if not self._armed:
                return
            self._armed = False
            if self._stop_timer:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(self.TAIL_SEC, self._finish_stop)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _finish_stop(self):
        with self._lock:
            self._accepting = False

            # 先把 VAD 里没说完的残段吐出来。这些同样走 _push，
            # 太短的会并进 pending，和后面的一起解。
            vad_parts: list[np.ndarray] = []
            try:
                self.vad.flush()
                while not self.vad.empty():
                    seg = self.vad.front
                    vad_parts.append(
                        np.array(seg.samples, dtype=np.float32).copy())
                    self.vad.pop()
            except Exception:
                pass
            for part in vad_parts:
                self._push(part)

            # VAD 一段都没吐（说得太短），才用 _acc 兜底。
            # 否则会和上面吐出来的内容重复，同一句识别两遍。
            if (not vad_parts and self._acc
                    and self._voiced > self.sample_rate // 5):
                self._push(np.concatenate(self._acc))

            self._reset_acc()
            self._flush_pending()
            try:
                self.vad.reset()
            except Exception:
                pass
            # 到这里尾巴都进队列了，才允许取数据的线程退出
            self._closing = True

    # 兼容旧调用名
    def shutdown(self) -> None:
        self.close()

    @staticmethod
    def list_devices():
        """列出可用的输入设备。

        sounddevice 默认把 MME / DirectSound / WASAPI 三套 API 的设备全列出来，
        同一个麦克风会重复出现三四次，还带一堆没解析的资源串
        （形如 Input (@System32\\drivers\\...,#4;%1 Hands-Free%0,;(iPhone))）。
        所以只留 WASAPI 那一套，再按名字去重。
        """
        import sounddevice as sd

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception:
            return []

        def api_name(idx):
            try:
                return (hostapis[idx] or {}).get("name", "")
            except Exception:
                return ""

        rows = []
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) <= 0:
                continue
            name = (d.get("name") or "").strip()
            # 没解析出来的资源串（@System32\...、%1 ... %0）、空括号、空名字，一律不要
            if (not name or "@" in name or "%" in name
                    or "()" in name or name.startswith("(")):
                continue
            rows.append((i, name, api_name(d.get("hostapi", -1))))

        def pick(*keywords):
            return [r for r in rows
                    if any(k in r[2].upper() for k in keywords)]

        out, seen = [], set()

        def take(group):
            for i, name, _api in group:
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append((i, name))

        # WASAPI 优先（名字干净、延迟低），WDM-KS 兜底（能拿到立体声混音）。
        # MME / DirectSound 是同一批设备的老接口副本，只有前面都空时才拿出来。
        take(pick("WASAPI"))
        take(pick("WDM-KS"))
        if not out:
            take(rows)
        return out
