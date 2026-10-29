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

        # 短段合并：不到 min_asr_sec 的段先攒着，跟下一段拼起来再送识别。
        # 识别模型是整段解码的，看不到别的上下文，片段越短同音字越容易错。
        self.min_asr_samples = int(
            float(vad_cfg.get("min_asr_sec", 3.0)) * sample_rate)
        self.max_segment_samples = int(
            float(vad_cfg.get("max_segment_s", 20)) * sample_rate)
        self.min_tail_samples = int(self.MIN_TAIL_SEC * sample_rate)
        self._pending: list[tuple[np.ndarray, int]] = []   # (音频, 距离上一段的静音采样数)
        self._last_end = 0      # 上一段在 VAD 输入流里的结束位置（采样点）

        # 整轮录音的全部音频（每段已削过首尾静音）。松键后拿它整段重解一次。
        # VAD 分段 → 每段独立解码，上下文在边界处断掉，跨边界的词两半各解一次
        # 就会丢字（线上日志实测：「长句子断句吧，个断句还是…」丢了「这」）。
        # 整段重解上下文完整，是这条链路的正解，代价是多 1~2 秒。
        self._utterance: list[np.ndarray] = []
        self._utt_lock = threading.Lock()

    def _emit_error(self, msg: str):
        if self.on_error:
            self.on_error(msg)

    def _reset_acc(self, keep_voiced: bool = False):
        """清空累积缓冲。

        keep_voiced 用于「VAD 切出一段」的情形：音频已经被 VAD 拿走了，
        必须清 _acc 免得重复，但**有声时长不能清** —— 清了的话，用户每
        停顿一次强制切分的计时就归零，一口气说很久也永远不触发。
        """
        self._acc = []
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
        """
        self._pending.append((np.asarray(samples, dtype=np.float32), gap))
        total = sum(len(p[0]) for p in self._pending)

        if total < self.min_asr_samples and not forced:
            return False                    # 太短，先攒着等下一段
        if total >= self.max_segment_samples:
            forced = True                   # 攒过头了，先送出去

        # 合并时 gap 取第一段的：它才是这段相对上一段的真实间隔，
        # 后面几段的间隔属于段内部，已经被并进同一段音频里了
        buf = (self._pending[0][0] if len(self._pending) == 1
               else np.concatenate([p[0] for p in self._pending]))
        first_gap = self._pending[0][1]
        self._pending = []
        # 削掉首尾静音再送。硬切片段常带着几秒静音，不削的话模型会拿它
        # 当引子往下编（日志里表现为把热词表念出来）
        buf = _trim_silence(buf, self.sample_rate)
        if len(buf) < int(0.20 * self.sample_rate):
            return False                # 削完不剩什么，说明这段基本是静音
        # 先攒进整轮缓冲，再入队：入队后取数线程可能立刻解完并通知收尾，
        # 那时候再补就漏了最后一段
        self._append_utterance(buf)
        self._segments.put((buf, forced, first_gap))
        return True

    def _append_utterance(self, buf: np.ndarray) -> None:
        if buf.size == 0:
            return
        with self._utt_lock:
            self._utterance.append(buf)

    def take_utterance(self) -> np.ndarray | None:
        """取走这一轮录音的全部音频，取完即清空。

        只有在 `_drain` 收尾之后调用才拿得全 —— 那时队列已空、`_closing`
        已置位，不会再有新段进来。
        """
        with self._utt_lock:
            parts, self._utterance = self._utterance, []
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _flush_pending(self):
        """松键时把攒着的零头也送出去。

        这里原先按 300ms 设了一道门槛：不到 300ms 的零头直接扔。结果就是
        「一句话末尾丢字」—— 0.3 秒中文能说一到两个字，用户说完最后一个字
        立刻松键，那半句就被当成「不值得识别的零头」扔掉了。
        _pending 里攒的都是 VAD 判过有声音的片段，不是静音，再短也该送。
        """
        if not self._pending:
            return
        buf = (self._pending[0][0] if len(self._pending) == 1
               else np.concatenate([p[0] for p in self._pending]))
        first_gap = self._pending[0][1]
        self._pending = []
        # 只挡真正的碎片：不到 60ms 的音发不出完整的字，送进去只会出乱码
        if len(buf) >= self.min_tail_samples // 5:
            self._append_utterance(buf)
            self._segments.put((buf, False, first_gap))

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
            try:
                self.vad.accept_waveform(samples)
                # 路径 1：VAD 判定的句子结尾
                while not self.vad.empty():
                    seg = self.vad.front
                    # seg.start 是这一段在输入流里的绝对采样位置（C API 明确
                    # 保证 lifetime 内单调递增）。减去上一段的结束位置，就是
                    # 这两段之间真实的静音长度 —— 断句最硬的依据。
                    seg_start = int(getattr(seg, "start", -1))
                    gap = -1
                    if seg_start >= 0:
                        if self._last_end > 0:
                            gap = max(0, seg_start - self._last_end)
                        self._last_end = seg_start + len(seg.samples)
                    sent = self._push(
                        np.array(seg.samples, dtype=np.float32).copy(), gap=gap)
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
                    # gap=0：强制切分意味着中间没有停顿，下一段要直接接上
                    self._push(np.concatenate(self._acc), forced=True, gap=0)
                    self._reset_acc()
                    # VAD 内部还攒着同一段，不 reset 会重复出一遍
                    self.vad.reset()
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
            self._reset_acc()
            self._pending = []
            # 新一轮：清掉上一轮留下的整段音频，否则重解会把两段话拼在一起
            with self._utt_lock:
                self._utterance = []
            # 新一轮从零开始算静音间隔。中途 vad.reset() 不清，那之后是
            # 「一口气继续往下说」，本来就该按无停顿处理
            self._last_end = 0
            # 把按键前的预缓冲倒进 VAD：这 400ms 就是以前丢掉的开头
            pre = list(self._preroll)
            self._preroll.clear()
            self._armed = True
            self._accepting = True
            if pre:
                self._acc.extend(pre)
                try:
                    for blk in pre:
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
