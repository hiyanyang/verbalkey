"""Verbalkey 入口。托盘常驻、热键录音、识别上屏、后台整理。"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QDialog, QMenu, QSystemTrayIcon,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr import AsrEngine  # noqa: E402
from src.audio import Recorder, _trim_silence  # noqa: E402
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

REPLACE_WINDOW_SEC = 45

# 整段重解的安全上限（秒）。实测（不带热词、6 线程 CPU）：
#   11.4s / 20.8s / 32.2s 都能完整解出（32.2s 得到 290 字，期望 292 字）
#   43.6s 开始被截断（282 字 / 期望 433 字）
#   79.7s 直接崩成一个幻觉 token「language」，一个字都不剩
# 所以取 30 秒留余量，超过就退回分段结果（不比现状差）。
# 注意：这个上限是 prompt + 音频共用 max_total_len(1024) 换来的，
# 将来若重新给解码 prompt 塞热词，可用音频会变短，这里的值要跟着降。
_MAX_REDECODE_SEC = 30.0


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
    segment = Signal(str, int)   # 文本, 距离上一段的静音采样数
    finished = Signal()
    state = Signal(str)
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
        self.pending_replace = None
        self._last_peak = 0.0
        # 记住按热键时焦点在哪个窗口，上屏前要把焦点还回去
        self.target_hwnd = get_foreground()
        self.busy = True
        self._prewarm_polish()
        self._make_recorder()
        if not self.recorder or not self.recorder.start():
            self.busy = False
            return
        self.hud.show_hud("recording", "")

    def _stop(self):
        if not self.busy or not self.recorder:
            return
        self.recorder.stop()
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
        vad_model = MODELS_DIR / "silero_vad.onnx"
        if not vad_model.exists():
            self.bridge.error.emit(f"缺少 VAD 模型: {vad_model}")
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
            text = self.asr.transcribe(samples, int(CONFIG.get("audio", "sample_rate")))
            log.info("识别 %.1fs 音频，峰值 %.3f，耗时 %.2fs%s%s",
                     len(samples) / 16000, self._last_peak, time.time() - t0,
                     "（硬切）" if forced else "",
                     f"，前置停顿 {gap / 16000:.2f}s" if gap and gap > 0 else "")
            if text:
                self.bridge.segment.emit(text, gap)
        except Exception as e:
            log.exception(e)
            self.bridge.error.emit(f"识别出错: {e}")

    def _on_segment_ui(self, text: str, gap: int = 0):
        self.buffer.append((text, gap))
        self.hud.set_text("".join(t for t, _ in self.buffer))
        self.hud.set_state("recording")

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

        want_polish = level >= 2
        # 「等整理好一次写进去」不只对书面化有意义。去口水词（level=1）这一档
        # 同样要整段重解一次来修分段边界丢的字，那 2~4 秒里先写草稿再替换，
        # 用户看到的就是文字跳变 + 光标被打断。所以只要后面还有结果要回来，
        # 就尊重这个开关，别管有没有跑大模型。
        has_later = want_polish or (level == 1 and utterance is not None)
        direct = has_later and CONFIG.get("polish", "paste_mode") == "polished"

        if direct:
            log.info("直写模式：草稿只走浮窗，等终稿再写入（级别 %d）", level)
            self.hud.show_hud("polishing", final)
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
                self.hud.flash("done", final, 30000)
                self.hud.set_hint("整段重解 + 整理中…")
            elif utterance is not None:
                # 没开整理、但还要整段重解。同样不能让浮窗先关掉再弹回来：
                # 重解 24 秒音频实测要 9.8 秒，1.6 秒的 flash 根本盖不住。
                # 用户实测投诉的就是这条路径（level=1 时窗口关了又冒出来）。
                self.hud.flash("done", final, 30000)
                self.hud.set_hint("整段重解中…")
            else:
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
        """
        sr = int(CONFIG.get("audio", "sample_rate"))
        # 整轮缓冲存的是原始音频（未削静音），里面含按键前 0.8 秒预缓冲和段间
        # 所有停顿。两头削一次：太长的静音引子会让模型顺着热词 prompt 往下编
        # （实测有一段前面拖了 6.5 秒静音）。只削两头，中间的停顿是真停顿。
        # 尾部补齐不在这里做 —— 那是解码层的事（asr._TAIL_PAD_SEC），
        # 放这儿会被削掉。
        clean = _trim_silence(utterance, sr)
        if len(clean) < len(utterance):
            log.info("整段重解前削掉首尾静音 %.2fs → %.2fs",
                     len(utterance) / sr, len(clean) / sr)
        # 上限按削过之后的长度算：说话的净长度才是模型要吃的量
        secs = len(clean) / max(sr, 1)
        if secs > _MAX_REDECODE_SEC:
            log.info("跳过整段重解：音频 %.1fs 超过 %.0fs 上限（再长会被模型截断）",
                     secs, _MAX_REDECODE_SEC)
            return draft
        if self.busy:
            log.info("跳过整段重解：已经在录下一段")
            return draft
        if not len(clean):
            return draft              # 整段都是底噪，没什么可解的
        t0 = time.time()
        try:
            text = self.asr.transcribe(clean, sr)
        except Exception as e:
            log.warning("整段重解失败，沿用分段结果：%s", e)
            return draft
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
        log.info("整段重解 %.1fs 音频，耗时 %.2fs：分段 %d 字 → 整段 %d 字",
                 secs, dt, len(draft), len(text))
        return text

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
    log.info("启动标记 BUILD=2026-10-30-r13 版本=%s 整段重解上限=%.0fs "
             "粘贴模式=%s 整理级别=%s 自动替换=%s",
             APP_VERSION,
             _MAX_REDECODE_SEC,
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
