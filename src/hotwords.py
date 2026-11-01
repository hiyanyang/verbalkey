"""热词自动生成：把用户真实说过的词喂回 ASR 的上下文偏置。

为什么要做这个
--------------
sherpa-onnx 的 qwen3_asr 支持 hotwords 参数（逗号分隔的字符串，会拼进解码
prompt），词在解码时被加权，专有词错误率能明显下降。但让用户一个个填不现实：
他自己也不知道哪些词容易说错。所以改成自动来。

数据从哪来
----------
不读 Rime 的 userdb。那是拼音打字的词频库，和"语音里说了什么"是两回事；
而且它是 LevelDB 二进制格式，读它得额外装依赖。直接用程序自己上屏过的文本
当语料，来源干净、格式简单、越用越准。

怎么挖词
--------
中文 NLP 通用的"凝固度 + 自由度"方法（互信息 + 左右熵）。
评分公式照抄 zhanzecheng/Chinese_segment_augment（594 stars）和
hammershock/WordDiscoveryNLP 的做法：

    score = PMI + min(左熵, 右熵)

PMI 衡量"这几个字抱团有多紧"，左右熵衡量"这个词换个上下文还能不能独立出现"。
两个都高才是真词。区别是我们不依赖 jieba：直接对 2~5 字字串做统计，
省掉一个分词器和一份几十 MB 的词典 —— 对"从几句口述里找业务词"这个场景，
字级 n-gram 已经够用。
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

HOTWORD_DIR = DATA_DIR / "hotwords"
# 存的是成对记录（每行一个 JSON）：{"a": 分段草稿, "f": 整段重解, "t": 时间}
# 以前只存"最终上屏的那一句"，只有结果没有对照，学不到模型听错了什么。
CORPUS_PATH = HOTWORD_DIR / "corpus.jsonl"
WORDS_PATH = HOTWORD_DIR / "words.txt"

# qwen3-asr 的 hotwords 是拼进 prompt 头部的，和音频 KV 抢同一个
# max_total_len(1024) 预算，而且会扰动解码。
#
# 实测（同一段音频、只改词表）：
#   无热词 → 141 字 / 15 个词 → 144 字 / 19 个词 → 0 字（整句塌掉）
#   42 个词 → 144 字，但占掉 145 个 token，长音频会被截断
# 词量和失败不是单调关系，说明这是 LLM 类 ASR 的退化，只能靠"少给词"压概率。
# 8 个词约占 30 token，是扰动和收益能兼顾的量级。
# 另外 asr.transcribe 里有兜底：解不出字会自动去掉热词重试，不会交白卷。
MAX_WORDS = 10
MAX_MANUAL = 8
MIN_FREQ = 2          # 出现 2 次就算候选。3 次太苛刻：业务词一天说不了一两遍，
                      # 门槛设 3 等于永远挖不出来，这个功能就白做了
MIN_PMI = 2.0         # 凝固度门槛
MIN_ENTROPY = 0.8     # 左右熵门槛（越小越像词的一部分，越难独立成词）
MAX_LEN = 5
CORPUS_LIMIT = 400_000  # 语料上限 40 万字，超了砍掉前面一半

_lock = threading.Lock()

# 内置词表：**空**。
# 之前这里写死了 6 个词（陪护/护工/护理员/压疮/鼻饲/翻身扣背），是开发时
# 自己按业务猜的，用户明确要求删掉 —— 不在没被要求的情况下往解码 prompt
# 里塞内容。热词完全由用户在设置页自己填，不填就不带任何热词。
_BASE: list[str] = []

# 高频虚词。左右熵很高、PMI 很低，理论上会被门槛挡掉大半，
# 但这两个指标挡不住"本来就常一起出现"的词组（比如"的时候""一个"），
# 所以还是列一份显式停用表。
_STOP = set("""
这个 那个 就是 然后 所以 但是 因为 如果 还是 已经 可以 可能
我们 你们 他们 她们 自己 什么 怎么 这样 那样 一样 一定 一直
时候 东西 事情 问题 地方 一下 一点 一些 这些 那些 现在 今天
明天 昨天 应该 需要 觉得 知道 看到 听到 说到 做到 得到 一个
两个 没有 不是 不能 不会 不要 其实 反正 另外 还有 同时 可是
于是 而且 不过 结果 接着 比如 例如 的话 了对 了一 是不 不是
这种 那种 每个 有些 很多 多少 几个 第一 第二 最后 之前 之后
拼音 输入法 拼音输入法 语音 识别 文字 文本 整理 设置 程序 软件 功能
""".split())

# 数字 + 量词拼出来的不是业务词。「十五秒」「两个月」这类会稳定被挖出来
# （口述里天然爱说时间），但模型对数字本来就不敏感，塞进热词只是白占名额、
# 还扩大词表回显的面。实测就挖出过「十五秒」，纯粹是噪音。
_NUM_UNIT = set("一二三四五六七八九十百千万零两秒分时天月年号点块元次位条岁斤克米度半几")

# 高频虚字。含这些字的组合基本不可能是热词 —— "个月""的时候""就是"
# 这类会稳定混进候选，光靠 PMI 和左右熵挡不住。
_FUNC_CHARS = set("个的了是在和就都也还又很太更最不没有这那哪什么怎样时候吧呢啊吗嘛哦呀呗")

_CN = re.compile(r"[\u4e00-\u9fff]+")
_SPLIT = re.compile(r"[,，、\s]+")


# ---------------------------------------------------------------- 语料

def _sentences(text: str) -> list[str]:
    """按标点把文本切成句子，只留含中文的。

    新词不能跨句统计 —— "说完了。然后" 这种跨句相邻会把两句的边界字
    拼成假词，左右熵还会被这种假相邻抬得很高。
    """
    out = []
    for seg in re.split(r"[^\u4e00-\u9fffA-Za-z0-9]+", text or ""):
        seg = seg.strip()
        if len(seg) >= 2:
            out.append(seg)
    return out


def record(asr: str, final: str) -> None:
    """记一对语料：分段草稿 + 整段重解。

    为什么要成对：只留最终那一句，只有结果、没有「模型原本听成什么」，
    学不到纠正，也挖不出「哪些字总在边界处被听错」。左边草稿、右边终稿，
    差异就是可学的信号（meya 那套热词学习的前提就是拿到改动差异）。

    只落本机，超 CORPUS_LIMIT 砍掉前半。失败一律吞掉 ——
    记录这种事绝不能影响上屏。
    """
    a = (asr or "").strip().replace("\n", " ")
    b = (final or "").strip().replace("\n", " ")
    if not b or len(b) < 2:
        return
    try:
        HOTWORD_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"a": a, "f": b, "t": int(time.time())},
                          ensure_ascii=False)
        with _lock:
            with CORPUS_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim_corpus()
    except Exception:
        pass


def _trim_corpus() -> None:
    """语料超上限就砍掉前面一半，保留最近的。调用方已持锁。"""
    try:
        if CORPUS_PATH.stat().st_size <= CORPUS_LIMIT:
            return
        lines = CORPUS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        keep = lines[len(lines) // 2:]
        CORPUS_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def corpus_size() -> int:
    """语料里「终稿」的总字数，给界面显示积累了多少。"""
    total = 0
    try:
        text = CORPUS_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    for line in text.splitlines():
        try:
            total += len(json.loads(line).get("f") or "")
        except Exception:
            continue
    return total


# ---------------------------------------------------------------- 挖词

def _entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for v in counter.values():
        p = v / total
        h -= p * math.log(p)
    return h


def _pmi(gram: str, freq: dict[str, int], n: int) -> float:
    """凝固度：这个词整体出现的概率，比它各部分独立出现高多少个数量级。

    多种切分要取最小值 —— "护工费" 如果按 "护"+"工费" 拆也成立、
    按 "护工"+"费" 拆也成立，两种拆法都黏得紧才算真词。
    """
    fg = freq.get(gram, 0)
    if fg <= 0:
        return -1e9
    best = 1e9
    for k in range(1, len(gram)):
        a, b = gram[:k], gram[k:]
        fa, fb = freq.get(a, 0), freq.get(b, 0)
        if fa <= 0 or fb <= 0:
            return -1e9
        v = math.log(fg * n / (fa * fb))
        if v < best:
            best = v
    return best if best < 1e9 else -1e9


def _top_n(n_chars: int) -> int:
    """语料越多，敢用的挖词越多。

    天花板压得很低：总名额只有 8 个，挖词再多也是把内置词顶掉，
    而内置词是精选过的。语料没积累够时干脆一个都不用。
    """
    if n_chars < 3_000:
        return 2
    if n_chars < 12_000:
        return 3
    if n_chars < 40_000:
        return 4
    return 5


def mine(corpus: str, top: int | None = None) -> list[str]:
    """从语料里挖候选词，返回按可信度排序的词表。"""
    chars = "".join(_sentences(corpus))
    n = len(chars)
    # 语料太少时统计毫无意义，直接返回空，让内置词表顶上。
    # 注意 n 是去掉标点数字之后的纯中文字数，大约是真人口述字数的 0.5 倍 ——
    # 说 20 句话就有 400 多字，够跑一轮了。
    if n < 350:
        return []

    # 第一遍：所有 1~MAX_LEN 字的字串频次。
    # 1-gram 必须留着，算 PMI 时要用它当分母。
    freq: dict[str, int] = {}
    for L in range(1, MAX_LEN + 1):
        c: Counter = Counter()
        for i in range(n - L + 1):
            c[chars[i:i + L]] += 1
        # 只留出现过 2 次以上的，压内存；单次出现的不可能是热词
        for g, v in c.items():
            if v >= 2 or L == 1:
                freq[g] = v

    # 只从 3 字以上的候选里挖。两字词绝大多数是「比较」「时候」这种通用词，
    # 模型本来就能听对，塞进热词只是白占名额；真正的两字业务词（陪护/护工/
    # 压疮/鼻饲）已经在 _BASE 里钉死了，不需要靠统计去发现。
    # 另外要求至少出现 3 次：只说过一两遍的词统计不可信。
    cands = [g for g, v in freq.items()
             if 3 <= len(g) <= MAX_LEN and v >= max(MIN_FREQ, 3)
             and _CN.fullmatch(g) and g not in _STOP
             and not (_FUNC_CHARS & set(g))
             and not (_NUM_UNIT & set(g))]
    cand_set = set(cands)

    # 第二遍：一遍扫过去，顺手给所有候选收集左右邻字。
    # 不能每个候选单独扫全文 —— 那样是 O(语料 × 候选数)，语料上万字就是
    # 上亿次比较，卡到没法用。
    left_d: dict[str, Counter] = defaultdict(Counter)
    right_d: dict[str, Counter] = defaultdict(Counter)
    for i in range(n):
        for L in range(2, MAX_LEN + 1):
            if i + L > n:
                break
            g = chars[i:i + L]
            if g not in cand_set:
                continue
            left_d[g][chars[i - 1] if i > 0 else ""] += 1
            right_d[g][chars[i + L] if i + L < n else ""] += 1

    scored: list[tuple[float, str]] = []
    for g in cands:
        h = min(_entropy(left_d[g]), _entropy(right_d[g]))
        if h < MIN_ENTROPY:
            continue
        p = _pmi(g, freq, n)
        if p < MIN_PMI:
            continue
        # 长词信息量更大，同分给点倾斜。倍率要克制：调太高会让 5 字的
        # 假串压过真正常用的 2~3 字词，挖出来全是没法用的长串
        bonus = 1.0 + 0.18 * (len(g) - 2)
        scored.append(((p + h) * bonus, g))

    scored.sort(key=lambda x: -x[0])

    # 去冗余，长词优先："护理员"比"护理"更具体，遇到就让长的顶掉短的。
    limit = top or _top_n(n)
    picked: list[str] = []
    for _, g in scored:
        if g in picked:
            continue
        swallowed = False
        for i, w in enumerate(picked):
            if w in g and len(w) < len(g):
                picked[i] = g
                swallowed = True
                break
            if g in w:
                swallowed = True
                break
        if not swallowed:
            picked.append(g)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------- 词表

def build(manual: str = "", mined: list[str] | None = None) -> str:
    """拼出最终给 ASR 的逗号串。

    只取「手动词 + 内置业务词」。自动挖的词**不进 prompt**。

    原因是用下来的实际数据：连续几天的语料挖出来的依次是
    「拼音输入法」「十五秒」「语音识别」「比较」「解决问题」——全是通用词。
    模型本来就能听对这类词，塞进 prompt 只会白占跟音频抢的 KV 预算，
    还会扩大「词表被念出来」的回显面（实测发生过两次）。
    自动挖词本身没有判词能力（没有通用词典，分不清「压疮」和「解决问题」），
    所以在这个能力补上之前，让它只做统计展示，不进解码。

    名额留给真正难听对的业务词：压疮、鼻饲、翻身扣背这类通用语料里几乎没有的。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(w: str) -> None:
        w = w.strip()
        if not w or w in seen or len(out) >= MAX_WORDS:
            return
        seen.add(w)
        out.append(w)

    for w in _SPLIT.split(manual or ""):
        if w and len(out) < MAX_MANUAL:
            add(w)
    for w in _BASE:
        add(w)
    return ",".join(out)


def cached() -> list[str]:
    """读上次挖好的词表。"""
    try:
        raw = WORDS_PATH.read_text(encoding="utf-8")
    except Exception:
        return []
    return [w for w in raw.split("\n") if w.strip()]


def refresh(manual: str = "") -> tuple[str, int]:
    """重新挖词并落盘，返回 (逗号串, 挖出的词数)。"""
    HOTWORD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        corpus = CORPUS_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        corpus = ""
    mined = mine(corpus)
    try:
        WORDS_PATH.write_text("\n".join(mined) + "\n", encoding="utf-8")
    except Exception:
        pass
    return build(manual, mined), len(mined)


# ---------------------------------------------------------- 从改正里学
#
# 为什么不用「自动挖词」：那套统计只判断"这是不是一个成词的单位"，判断不了
# "这是不是一个模型会听错的业务词"。实测挖出来全是「语音识别」「比较」这类
# 通用词，塞进解码 prompt 只会挤占音频预算，还会被模型念出来。
#
# 改成从**改正**里学：同一段音频，分段解码（草稿）和整段重解（终稿）会给出
# 不同结果，差异处就是"模型在这个位置听错了什么"。同一处差异出现两次以上
# 才收录，存成「听错的形式 → 正确的形式」。
#
# 关键取舍：学到的词**不塞进解码 prompt**，只在识别完成后做一次文本替换。
# 这样零风险（不会幻觉、不占音频预算），误伤概率也低（只有原字串原样出现
# 才替换，且只收 2 字以上）。代价是它纠正不了"整词听成完全不相干的词"
# 之外的发音问题 —— 那不是文本替换能解决的。

LEARNED_PATH = HOTWORD_DIR / "learned.json"
MIN_LEARN = 2      # 同一处纠正出现两次才收录，一次性偶发不算
# 只收 2 字以上的替换。单字错听（实测「轮胎」被听成「它」）**文本替换救不了**：
# 把「它」全局换成「轮胎」会毁掉所有句子。这类只能靠模型，不该硬做。
MIN_LEN = 2
MAX_LEN = 6
# 整对文本相似度低于这个值，说明不是同一句的修正，而是改写 —— 整对丢弃
MIN_PAIR_RATIO = 0.60

_learn_cache: dict | None = None
_learn_mtime = 0.0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _is_cn(s: str) -> bool:
    return bool(s) and all("\u4e00" <= ch <= "\u9fff" for ch in s)


def _ok_token(s: str) -> bool:
    """这条差异值不值得学。

    原来只认纯汉字，**英文词一条都进不来** —— 实测语料里五条含 GitHub 的
    记录，草稿侧分别写成「给他们」「略微」「skill」，全都因为
    `_is_cn('GitHub') == False` 被丢弃，用户手动改了三次也没学会。
    现在放开到「纯汉字 或 纯 ASCII 字母数字」，中英混排（如「review人」）
    仍然拒绝，那种替换面太大。
    """
    if not s:
        return False
    if _is_cn(s):
        return True
    return all(ch.isascii() and (ch.isalnum()) for ch in s)


def _diff_spans(a: str, b: str) -> list[tuple[str, str]]:
    """对齐草稿和终稿，取出「听错 → 听对」的连续片段。

    两道守卫，都是实测撞出来的：

    1. **整对相似度低于 MIN_PAIR_RATIO 直接放弃**。只按片段长度过滤挡不住
       整句改写 —— 实测「这句话我想说的是另外一个意思」对「完全换了一种说法
       的表述」，对齐后会切出一个 5 字的替换片段，长度完全合法。这类差异
       不是"听错"，学进去就是污染。
    2. **相邻差异段间隔 ≤2 字就合并**。不合并的话「它上上面」会被切成
       「它上」→「轮胎」，留下的 「上面」因为相同被跳过，最终映射成了
       一个只有 2 字的怪词；合并后是完整的「它上上面」→「轮胎上面」。
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < MIN_PAIR_RATIO:
        return []
    regs = [(i1, i2, j1, j2)
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    merged: list[tuple[int, int, int, int]] = []
    for i1, i2, j1, j2 in regs:
        if (merged and i1 - merged[-1][1] <= 2 and j1 - merged[-1][3] <= 2):
            p1, _, p3, _ = merged[-1]
            merged[-1] = (p1, i2, p3, j2)
        else:
            merged.append((i1, i2, j1, j2))

    out: list[tuple[str, str]] = []
    for i1, i2, j1, j2 in merged:
        wrong, right = a[i1:i2], b[j1:j2]
        if not (MIN_LEN <= len(wrong) <= MAX_LEN and MIN_LEN <= len(right) <= MAX_LEN):
            continue
        if not (_ok_token(wrong) and _ok_token(right)):
            continue
        out.append((wrong, right))
    return out


def learn() -> dict[str, dict]:
    """扫语料，把反复出现的纠正收进 learned.json。返回对照表。"""
    global _learn_cache, _learn_mtime
    counter: dict[tuple[str, str], int] = {}
    try:
        text = CORPUS_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return learned()
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        a, b = rec.get("a") or "", rec.get("f") or ""
        if not a or not b or a == b:
            continue
        for wrong, right in _diff_spans(a, b):
            counter[(wrong, right)] = counter.get((wrong, right), 0) + 1

    table: dict[str, dict] = {}
    for (wrong, right), n in counter.items():
        if n < MIN_LEARN:
            continue
        cur = table.get(wrong)
        # 同一个错听形式对应多种改法时，取出现最多的那个
        if cur is None or n > cur["n"]:
            table[wrong] = {"to": right, "n": n}

    # 去掉冗余：某个键里包含了另一个更短的键，且短键的改法也出现在长键的改法里，
    # 就只留短键（更通用）。实测会同时学到「树杈→车刹」和「到这个树杈→倒着这个车刹」，
    # 后者是前者的子串，留着只是噪音。
    for k in list(table):
        for short in table:
            if short == k or len(short) >= len(k):
                continue
            if short in k and table[short]["to"] in table[k]["to"]:
                del table[k]
                break
    try:
        HOTWORD_DIR.mkdir(parents=True, exist_ok=True)
        LEARNED_PATH.write_text(
            json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    _learn_cache, _learn_mtime = table, _mtime(LEARNED_PATH)
    return table


def learned() -> dict[str, dict]:
    """读对照表；文件变了才重读，避免每句话都解析 JSON。"""
    global _learn_cache, _learn_mtime
    m = _mtime(LEARNED_PATH)
    if _learn_cache is None or m != _learn_mtime:
        try:
            _learn_cache = json.loads(LEARNED_PATH.read_text(encoding="utf-8"))
        except Exception:
            _learn_cache = {}
        _learn_mtime = m
    return _learn_cache or {}


def apply_corrections(text: str) -> str:
    """把学到的「听错形式」替换成正确形式。长串优先，避免短的先替换破坏长的。"""
    table = learned()
    if not text or not table:
        return text
    hits: list[str] = []
    for wrong in sorted(table, key=len, reverse=True):
        if wrong and wrong in text:
            right = (table[wrong] or {}).get("to") or ""
            if right and wrong != right:
                text = text.replace(wrong, right)
                hits.append(f"{wrong}→{right}")
    if hits:
        log.info("应用学到的纠正 %d 处：%s", len(hits), "、".join(hits))
    return text


# ------------------------------------------------ 按热词做同音 / 近形修正
#
# 为什么在解码热词之外还要再来一层：
#
#   1. 解码热词是拼进 prompt 的，和音频抢同一个 max_total_len(1024)，
#      所以只能给 8 个（见 MAX_MANUAL 上方的实测）；
#   2. 词给多了解码会整体退化，实测 19 个词直接把整句解成 0 字；
#   3. **给了热词也会偶尔听错**：用户实测「github」手动改过两三次，
#      之后仍然复发。热词只是提高概率，不是保证。
#
# 所以在输出之后再加一道纯文本修正：不进解码、不占 token、不限数量。
# 业界也是这么分的 —— FunASR 文档把 hotword（上下文偏置，原文「不保证
# 一定插入指定词，也不是确定性文本替换」）和 postprocess_hotwords
# （显式映射替换）做成两个不同参数，并且注明模糊匹配要用 pypinyin。
#
# 两条路，原则都是**宁可漏修，不误改**：
#
#   中文：无声调拼音逐个相等才换。热词「压疮」是 ya1 chuang1，错听成的
#         「压床」是 ya1 chuang2 —— 声调不同，但无声调一样。模型听错时
#         往往连声调一起错，卡住声调就什么都修不了，所以这里不卡。
#
#   英文：去掉空格后**字符数必须完全相同**，且相似度 ≥ 0.82。
#         为什么非要卡等长 —— 实测 skills→skill 相似度 0.909、
#         markdownn→markdown 0.941，这两个都是合法写法，改了就是错。
#         代价是 githb→github（差一个字母）修不了。漏修能接受，误改不行。
#
# 中文不足 2 字的不参与（单字替换面太大，会毁整句）。

_PY_OK = False
try:                                    # pypinyin 是可选依赖：
    from pypinyin import lazy_pinyin, Style   # 打包漏了它也不能让程序起不来
    _PY_OK = True
except Exception:                       # pragma: no cover
    lazy_pinyin = None                  # type: ignore
    Style = None                        # type: ignore

_EN_TOL = 0.82      # 英文近形相似度下限
_CN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_EN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._\-]*[A-Za-z0-9]|[A-Za-z0-9]")


def _py_chars(s: str, tone: bool = False) -> list[str]:
    """逐字拼音，长度与 s 一致。拿不到拼音就返回空。"""
    if not _PY_OK:
        return []
    try:
        got = lazy_pinyin(s, style=Style.TONE3 if tone else Style.NORMAL,
                          errors=lambda x: list(x))
    except Exception:
        return []
    return got if len(got) == len(s) else []


def _is_ascii_word(s: str) -> bool:
    return bool(s) and s.isascii() and any(c.isalpha() for c in s)


def _en_close(a: str, b: str) -> bool:
    """a 是不是 b 的近形错拼（等长、去空格、相似度够）。

    特例：去掉空格后完全相等，说明拼写本身没错、只是被听成了分开的两三个
    词（实测「mark down」→ markdown）。这种情况要合并，但**只有原文里真有
    空格时才算** —— 否则「GitHub」和热词「github」只差大小写，也会被改成
    用户填的写法，等于无端改动用户文本。
    """
    x, y = a.replace(" ", "").lower(), b.replace(" ", "").lower()
    if not x or not y:
        return False
    if x == y:
        return a != b and " " in a
    if len(x) != len(y):
        return False
    if x[0] != y[0]:
        return False
    return difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() >= _EN_TOL


def _cn_spans(text: str, word: str) -> tuple[list[tuple[int, int]], list[str]]:
    """找和 word 同音的片段。

    返回 (自动改的区间, 只提示不改的说明)。

    分级的原因是一次实测：热词「护理」hu4 li3 会把「互利共赢」hu4 li4
    改成「护理共赢」—— 两个都是真词，只是声调不同。文本上看不出哪个是
    用户想说的，硬改就是毁句子。所以：

      * 声调也完全一致  → 自动改（同音同调的真词碰撞极少）
      * 只无声调一致    → 只记日志，不动文本。用户能看到「功能在工作」，
                          也能自己判断要不要把这个词当成错听处理。

    代价要说清：「压床」ya1 chuang2 vs「压疮」ya1 chuang1 只差声调，
    这种最常见的同音选字错**不会被自动修正**，只会出现在日志里。
    """
    py_w = _py_chars(word, tone=True)
    py_flat_w = _py_chars(word, tone=False)
    n = len(word)
    if not py_w or n < 2:
        return [], []
    spans: list[tuple[int, int]] = []
    hints: list[str] = []
    for m in _CN_RUN.finditer(text):
        run, base = m.group(0), m.start()
        if len(run) < n:
            continue
        pys = _py_chars(run, tone=True)
        flat = _py_chars(run, tone=False)
        if not pys:
            continue
        i = 0
        while i + n <= len(run):
            seg = run[i:i + n]
            if seg == word:             # 已经是对的
                i += 1
                continue
            if pys[i:i + n] == py_w:
                spans.append((base + i, base + i + n))
                i += n                  # 命中就整段跳过，避免重叠替换
                continue
            if flat and py_flat_w and flat[i:i + n] == py_flat_w:
                note = f"{seg}≈{word}"
                if note not in hints:
                    hints.append(note)
            i += 1
    return spans, hints


def _en_spans(text: str, word: str) -> list[tuple[int, int]]:
    """在文本里找 word 的近形错拼，返回区间列表（不重叠）。

    要处理「get hub」这种被听成两个词的情况，所以按空格切 token 后再
    把相邻 token 拼起来比 —— 拼之前先去掉空格，正好对上 _en_close。
    """
    out: list[tuple[int, int]] = []
    wlen = len(word)
    for m in _EN_RUN.finditer(text):
        seg = m.group(0)
        base = m.start()
        # token 起止位置，比按空格切更准（连续空格、首尾空格都不怕）
        toks = [(base + t.start(), base + t.end()) for t in re.finditer(r"\S+", seg)]
        taken = 0
        for i in range(len(toks)):
            for j in range(i, min(len(toks), i + 4)):
                cand = text[toks[i][0]:toks[j][1]]
                if len(cand.replace(" ", "")) > wlen + 1:
                    break
                if _en_close(cand, word):
                    if toks[i][0] >= taken:     # 不与已命中的重叠
                        out.append((toks[i][0], toks[j][1]))
                        taken = toks[j][1]
                    break
    return out


def phonic_fix(text: str, words: list[str]) -> tuple[str, list[str]]:
    """按热词做同音 / 近形修正。返回 (新文本, 命中说明)。

    words 是**全部**手动词，不受 MAX_MANUAL 限制 —— 这是这个函数存在的
    意义：超过 8 个的词进不了解码 prompt，但仍然能在这里生效。
    """
    if not text or not words:
        return text, []
    hits: list[str] = []
    hints: list[str] = []
    # 长词优先：短词先换会把长词的落点破坏掉
    for w in sorted({w.strip() for w in words if w}, key=len, reverse=True):
        if len(w) < 2:
            continue
        if _is_ascii_word(w):
            spans = _en_spans(text, w)
        elif _is_cn(w):
            spans, h = _cn_spans(text, w)
            for x in h:
                if x not in hints:
                    hints.append(x)
        else:
            continue                    # 中英混排的热词不参与，替换面说不清
        for a, b in reversed(spans):
            old = text[a:b]
            if old == w:
                continue
            text = text[:a] + w + text[b:]
            hits.append(f"{old}→{w}")
    if hits:
        log.info("热词同音修正 %d 处：%s", len(hits), "、".join(hits))
    if hints:
        log.info("热词同音但声调不同，未改（要改请手动）：%s", "、".join(hints))
    return text, hits


def phonic_ready() -> bool:
    """同音修正能不能用：既要 pypinyin 能 import，也要字典真的算得出拼音。

    只查 import 不够 —— 打包漏了 pinyin_dict.json 时 import 照样成功，
    但每次算拼音都会抛异常、被 _py_chars 吞成空列表，中文热词一条都改不了，
    表面上却一切正常。所以这里实算一次。
    """
    return _PY_OK and bool(_py_chars("压疮"))


def manual_list(manual: str) -> list[str]:
    """解析设置页填的热词串，返回去重后的完整词表（不截断）。"""
    out: list[str] = []
    seen: set[str] = set()
    for w in _SPLIT.split(manual or ""):
        w = w.strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def post_fix(text: str, manual: str = "") -> str:
    """输出后的统一修正入口：先学到的精确替换，再按热词同音修正。"""
    text = apply_corrections(text)
    words = manual_list(manual)
    if words:
        try:
            text, _ = phonic_fix(text, words)
        except Exception as e:
            log.warning("热词同音修正失败（忽略）：%s", e)
    return text


def for_asr(manual: str = "") -> str:
    """给 ASR 用的入口，返回逗号分隔的热词串。

    不再在加载时挖词：挖出来的都是通用词，进 prompt 只有坏处（见 build 的说明）。
    这里顺手刷一遍「从改正里学」的对照表 —— 它是纯文本替换，不进 prompt，
    所以刷新是安全的。
    """
    try:
        learn()
    except Exception:
        pass
    return build(manual)


def stats() -> dict:
    """给设置页显示的状态。"""
    mined = cached()
    chars = corpus_size()
    return {
        "chars": chars,
        # 挖词要纯中文 350 字（原文约 700 字），这里按原文口径报给界面
        "ready": chars >= 600,
        "need": max(0, 600 - chars),
        "mined": len(mined),
        "learned": len(learned()),
        "words": build().split(",") if build() else [],
    }


if __name__ == "__main__":
    # 手工验证：喂一段真口语，看能不能挖出业务词
    sample = """
    今天我们上门给一个术后老人做陪护，护理员到了之后先看了压疮的情况，
    家属说之前在医院是鼻饲，回家之后要改成经口喂。翻身扣背这个动作
    护理员每天要做六次。这个月嫂的单子转成了长期陪护，护理费按天结。
    做抖音短视频的话，选题要围绕护工上门这个场景，完播率才起得来。
    """ * 6
    got = mine(sample)
    print("挖出:", got)
    print("最终:", build("", got))
