"""识别结果里「不该出现的东西」：控制符清理 + 草稿可信度判定。

背景（2026-10-31 用户截图复现）
------------------------------
用户在草稿浮窗里看到 `<|endoftext|>Human Rights Watch（人权观察）是……`
这样一整段根本没说过的话，整段重解后消失。这是 Qwen3-ASR 这类生成式
模型的「静音/噪声幻觉」：音频里没有可听的内容时，模型按训练语料里最高频
的句式往下编。sherpa-onnx 上游已承认（PR #3907 修的是「整段静音」），但
「有噪声、没人声」这一种没修，也修不了 —— 模型判「这段没内容」靠的是
波形能量，噪声也有能量。

本模块只做两件事，且都很窄
--------------------------
1. `scrub()`：删掉控制符。`<|endoftext|>` 是模型的「我说完了」标记，它后面
   的一切都是模型自说自话。这东西绝不可能出现在人话里，所以删它是零误伤，
   而且对**所有**出口（草稿、终稿、剪贴板）都生效。

2. `judge()`：判断一段草稿「要不要显示」。**只影响浮窗显示，不改上屏文本**。
   判据是实测挑出来的，只用站得住的两条（见下）。判成可疑的段：不显示，
   但照样进 buffer，终稿重解会把它补回来。

**实测排除掉的判据**（2026-10-31，本机模型 + test_wavs）
    「字数 ÷ 有声秒数 > 9」→ 正常口播 fast1 是 13.2 字/秒，比幻觉样本
    （noise2 5.3、f1_noise 11.2）还高。会误杀真人说的话，不能用。
    「VAD 语音占比低」→ 纯噪声被判 96.5% 是语音（正常语音 91~97%），不能用。
    「音量低」→ 正常 26.7% vs 噪声 27.1%，不能用。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# 模型「我说完了」的标记。它之后的内容一律丢弃。
_TERMINATORS = ("<|endoftext|>", "<|im_end|>", "<|eot_id|>", "<|end|>")

# 其余控制符（`<|zh|>`、`<|transcribe|>` 这类语言/任务标签）：只删标记本身，
# 后面的正文留着 —— 它们出现在开头是正常的，不代表后面是编的。
_META_RE = re.compile(r"<\|[^|<>]{0,40}\|>")

# 整段输出只有这些词 = 模型吐了内部标记，不是人话。
# 实测 noise1-en.wav（88 秒纯噪声）解码结果就是单独一个「language」。
_JUNK_WORDS = {"language", "ltk", "soe", "unk", "nospeech", "non-speech", "sil"}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def scrub(text: str) -> str:
    """删掉控制符，并砍掉终止标记之后的内容。

    返回清理后的文本（可能为空）。空串表示「这一段全是模型自说自话」。
    """
    t = text or ""
    if "<|" not in t and "|>" not in t:
        return t

    cut_at = len(t)
    for tok in _TERMINATORS:
        i = t.find(tok)
        if i >= 0:
            cut_at = min(cut_at, i)
    head = t[:cut_at]
    tail_dropped = t[cut_at:]

    cleaned = _META_RE.sub("", head)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if tail_dropped.strip(" \t\r\n"):
        log.warning("识别结果含结束标记，砍掉其后 %d 字：%s",
                    len(tail_dropped.strip()), tail_dropped.strip()[:60])
    elif tail_dropped:
        log.warning("识别结果含结束标记（其后为空）")
    if cleaned != head.strip():
        log.warning("识别结果含控制符，已清除：%s", head.strip()[:60])
    return cleaned


def judge(text: str, expect_cjk: bool) -> str:
    """判断这段草稿可不可信。返回不可信的原因；空串表示可信。

    expect_cjk：这一轮之前有没有解出过中文。有，才允许启用「整段不是中文」
    这条判据 —— 反过来（用户整场在讲英文）就不会触发，避免误杀。

    判据一：整段只有一个内部标记词（language / ltk …）。
    判据二：前面说的是中文，这一段一个汉字都没有、却有 3 个以上英文字母
            —— 实测 f1_noise.wav（19 秒噪声）解码成 `Okay, Charles. It
            looks like we ha…`，正是这个形态。
    """
    t = (text or "").strip()
    if not t:
        return ""
    core = re.sub(r"[^0-9A-Za-z]", "", t).lower()
    if core in _JUNK_WORDS:
        return "整段是模型的内部标记词"
    if expect_cjk and not _CJK_RE.search(t):
        if len(_LATIN_RE.findall(t)) >= 3:
            return "整段没有汉字"
    return ""


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))
