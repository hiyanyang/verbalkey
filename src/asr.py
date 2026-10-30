"""ASR 引擎封装。只依赖 sherpa-onnx，换模型只需换目录。"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
import sherpa_onnx

from src import halluc

log = logging.getLogger(__name__)

# 连续出现这么多个热词，就判定成「把词表念出来了」而不是真在说话。
# 真实日志里是 8 个、9 个连着出现，正常口述不会这么排。
_ECHO_MIN_RUN = 4

# 解码前在音频尾部补这么长静音（秒）。
#
# 为什么需要：VAD 自然断句和「每 3 秒硬切」给出的段，实测段尾静音都是 0ms ——
# 结束点正好停在最后一个有声帧上，最后一个字的尾音（鼻音/塞音收尾）本来就在
# 帧边界之外。模型拿不到它就只能按语言先验猜，猜错就是「最后一个字丢了」。
# sherpa-onnx 的 Whisper 接口有个 tail_paddings 参数，官方文档原话是中文要
# 调到 300 帧才不截断句尾；Qwen3 接口没有这个参数，只能在音频层补。
#
# 补多少是实测扫出来的（4 段中文音频，同一套模型 + VAD，用 CER 里的删除数
# 看丢字、CER 看整体错误率）：
#   不补    CER 17.6%  删16
#   0.2s    CER 15.0%  删 9
#   0.4s    CER 14.2%  删10
#   0.6s    CER 12.4%  删 9     ← 最优
#   0.8s    CER 16.1%  删 6     ← 开始反噬（长静音又把模型带偏）
#   1.0s    CER 17.5%  删 4
# 所以不要再往上调；要改必须重跑 tools/probe_tail9.py 对比。
_TAIL_PAD_SEC = 0.60


def _hot_token(tok: str, words: set[str]) -> bool:
    """这个片段算不算热词的一部分。

    既要认完整热词（陪护、翻身扣背），也要认被模型拆开的半截
    —— 日志里「翻身扣背」被念成了「翻身、扣背」两段。
    """
    if not tok:
        return False
    if tok in words:
        return True
    return any(tok in w for w in words) and len(tok) >= 2


def _strip_hotword_echo(text: str, hotwords: str) -> str:
    """剪掉「热词表被当成正文念出来」的那一串。

    qwen3-asr 把热词拼进解码 prompt。遇到接近静音的音频段时，模型会顺着
    prompt 往下接，把整张词表复述出来 —— 词是用户自己给的，所以看起来
    像凭空冒出来的幻觉。实测两句：

      「…太积极太多了。系统，陪护 护工 护理员 压疮 鼻饲 翻身扣背 拼音输入法 十五秒。」
      「…是不是你哪儿写的，陪护、护工、护理员、压疮、鼻饲、翻身、扣背、拼音输入法、十五秒。」

    只在**连续 ≥4 个热词**时动手，正常说话里提到一次「陪护」必须原样保留。
    """
    words = {w.strip() for w in (hotwords or "").split(",") if w.strip()}
    if not text or len(words) < _ECHO_MIN_RUN:
        return text

    # 保留分隔符切开，方便按原样拼回去
    parts = re.split(r"([，、。！？；：,\s]+)", text)
    toks = [(i, p) for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    hot = [_hot_token(t, words) for _, t in toks]

    drop: set[int] = set()
    i = 0
    while i < len(toks):
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < len(toks) and hot[j]:
            j += 1
        if j - i >= _ECHO_MIN_RUN:
            for k in range(i, j):
                drop.add(toks[k][0])
            # 连词带它后面的分隔符一起丢，别留下「，，」
            for k in range(i, j):
                sep = toks[k][0] + 1
                if sep < len(parts):
                    drop.add(sep)
        i = j

    if not drop:
        return text
    out = "".join(p for i, p in enumerate(parts) if i not in drop)
    out = re.sub(r"([，、。！？；：,\s])\1+", r"\1", out)
    out = out.strip(" ，、；：,;:")
    if not out.endswith(("。", "！", "？", ".", "!", "?")) and out:
        out += "。"
    # 剪完只剩标点或一两个字，说明整段都是词表回显，交白卷比交噪音好
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", out)) < 2:
        log.warning("整段都是热词回显，丢弃：%s", text[:40])
        return ""
    log.warning("剪掉热词回显：%s", text[:60])
    return out


def _find(root: Path, *names: str) -> Path | None:
    """在模型目录里找指定文件，兼容不同的打包结构。

    names 按优先级给出候选文件名，命中即返回。先查一层子目录（很多包把模型
    放在子目录里），再递归全查。
    """
    hits: list[Path] = []
    for name in names:
        direct = root / name
        if direct.exists():
            return direct
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and (sub / name).exists():
                return sub / name
        for p in root.rglob(name):
            hits.append(p)
    return hits[0] if hits else None


class AsrModel:
    """一个可加载的 ASR 模型。目录里需要 conv_frontend/encoder/decoder + tokenizer。"""

    # 同一个权重的多种命名：fp32 / int8 / 量化后缀
    ENCODER_NAMES = ("encoder.int8.onnx", "encoder.onnx")
    DECODER_NAMES = ("decoder.int8.onnx", "decoder.onnx")
    FRONTEND_NAMES = ("conv_frontend.int8.onnx", "conv_frontend.onnx")

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self.name = self.dir.name

    def is_valid(self) -> bool:
        return all([self.conv_frontend, self.encoder, self.decoder])

    @property
    def conv_frontend(self):
        return _find(self.dir, *self.FRONTEND_NAMES)

    @property
    def encoder(self):
        return _find(self.dir, *self.ENCODER_NAMES)

    @property
    def decoder(self):
        return _find(self.dir, *self.DECODER_NAMES)

    @property
    def tokenizer(self):
        """tokenizer 目录：包含 vocab.json / merges.txt 的那个目录。"""
        for cand in ("vocab.json", "merges.txt", "tokenizer.json"):
            f = _find(self.dir, cand)
            if f:
                return f.parent
        return self.dir

    def size_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.dir.rglob("*") if f.is_file())


class AsrEngine:
    def __init__(self):
        self.recognizer = None
        self.model: AsrModel | None = None
        self.load_seconds = 0.0
        # 当前生效的热词 / 构造参数，transcribe 兜底重试时要用
        self.hotwords = ""
        self._args: tuple | None = None
        # 重载模型和识别是两条线程：一个 unload 把 recognizer 置 None 的瞬间，
        # 另一条正在 decode 就会 AttributeError。所有访问都过这把锁。
        self._lock = threading.RLock()

    def load(self, model_dir, num_threads: int = 4, hotwords: str = "",
             provider: str = "cpu") -> float:
        m = AsrModel(Path(model_dir))
        if not m.is_valid():
            raise FileNotFoundError(f"模型目录不完整: {m.dir}")

        t0 = time.time()
        # 构造最慢（几秒），放在锁外，别把识别线程卡死
        rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(m.conv_frontend),
            encoder=str(m.encoder),
            decoder=str(m.decoder),
            tokenizer=str(m.tokenizer),
            num_threads=num_threads,
            provider=provider,
            decoding_method="greedy_search",
            hotwords=hotwords or "",
            max_total_len=1024,
            # 上限不是越大越慢，只影响能吐多长。段长放宽到 20 秒后
            # 256 会在长句上截断（20 秒中文约 250 字，正常要 400+ token）
            max_new_tokens=512,
            # 温度从库默认的 1e-06 提到 0.05。这是为了「卡带」，不是为了
            # 让结果有随机性：
            #
            # sherpa 的解码每步只选概率最高的那个词。1e-06 相当于完全确定 ——
            # 一旦某一步滑进「重复上一个词」，之后每一步都还是同一个词，
            # 没有任何力量能把它拉出来。库内虽然检测到重复会截断，但截断后
            # 继续生成、又滑进去，一直撞到 max_new_tokens(512) 才停 ——
            # 实测因此把 16 秒的音频解成了 640 字、耗时 90 秒（正常 65 字 / 6 秒）。
            #
            # 0.05 的温度让每一步有极小概率选到别的词，死循环就能断开。
            # 实测（2026-10-30）8 段中文素材逐字对比：1e-06 与 0.05 的
            # 输出**完全一致**，不影响准确率。
            temperature=0.05,
        )
        with self._lock:
            self.recognizer = rec
            self.model = m
            # 记下构造参数：热词导致解码塌掉时要靠它重建一个不带热词的识别器
            self._args = (model_dir, num_threads, hotwords or "", provider)
            self.hotwords = hotwords or ""
        self.load_seconds = time.time() - t0
        return self.load_seconds

    def unload(self) -> None:
        with self._lock:
            self.recognizer = None
            self.model = None

    @property
    def ready(self) -> bool:
        return self.recognizer is not None

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        text = self._decode(samples, sample_rate)
        if text:
            # 热词表被当成正文念出来的情况，必须先剪掉再交出去 ——
            # 那串词是用户自己给的，看起来就像识别出了幻觉
            return _strip_hotword_echo(text, self.hotwords)
        if not self.hotwords:
            return text
        # 一个字都没解出来，但音频里确实有声音 —— 这是 LLM 类 ASR 的退化：
        # 热词拼进 prompt 后，某些词表组合会让解码整体塌掉、直接吐空。
        # 实测 19 个词的词表就复现过：同一段音频，不带热词出 141 字，
        # 带上直接 0 字。这比识别错几个字严重得多，用户说完话什么都没出来。
        # 所以兜一道：去掉热词重建识别器再解一次。慢几秒，但不会交白卷。
        peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        if peak < 0.02:
            return text            # 本来就没声音，不是解码的锅
        args = self._args
        if not args:
            return text
        log.warning("热词导致解码为空（峰值 %.3f），去掉热词重试", peak)
        model_dir, threads, _hw, provider = args
        try:
            # 重建后 self._args 里的 hotwords 会变成空串，同一组词不会再拖累下一句
            self.load(model_dir, threads, "", provider)
        except Exception as e:
            log.error("重建识别器失败: %s", e)
            return text
        return self._decode(samples, sample_rate)

    def _decode(self, samples: np.ndarray, sample_rate: int) -> str:
        # 整段解码期间持锁：宁可让「重载模型」多等一两秒，
        # 也不能在解码中途把 recognizer 抽走
        with self._lock:
            if self.recognizer is None:
                return ""
            x = np.asarray(samples, dtype=np.float32)
            if x.size:
                # 尾部补静音，见 _TAIL_PAD_SEC：不补的话最后一个字的尾音
                # 正好落在音频边界外，模型只能按语言先验猜，猜错就是丢字
                x = np.concatenate(
                    [x, np.zeros(int(sample_rate * _TAIL_PAD_SEC),
                                 dtype=np.float32)])
            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, x)
            self.recognizer.decode_stream(stream)
            # 控制符在这里就清掉：`<|endoftext|>` 是模型的「我说完了」标记，
            # 它后面的一切都是模型自说自话（2026-10-31 草稿里那段「人权观察」
            # 就是这么来的）。人话里不可能出现这种符号，删它零误伤，
            # 而且所有出口（草稿、终稿、剪贴板）都过这里，一处清全部干净。
            return halluc.scrub((stream.result.text or "").strip())

    def transcribe_batch(self, chunks: list, sample_rate: int = 16000) -> list[str]:
        """多段一起解码。保留供离线批处理用；实时链路实测并行并不比串行快
        （单段已吃满线程），所以主链路仍走 transcribe。"""
        if not self.ready or not chunks:
            return []
        streams = []
        for c in chunks:
            st = self.recognizer.create_stream()
            st.accept_waveform(sample_rate, np.asarray(c, dtype=np.float32))
            streams.append(st)
        self.recognizer.decode_streams(streams)
        return [(st.result.text or "").strip() for st in streams]


def scan_models(models_dir: Path):
    """扫描 models 目录下所有合法 ASR 模型。"""
    out = []
    root = Path(models_dir)
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        m = AsrModel(d)
        if m.is_valid():
            out.append(m)
        else:
            # 有些包会把模型放在子目录里
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and AsrModel(sub).is_valid():
                    out.append(AsrModel(sub))
                    break
    return out
