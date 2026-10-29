"""识别结果的文本后处理：繁转简、自定义替换、去重复标点。"""

from __future__ import annotations

import re

_T2S = None


def _get_converter():
    global _T2S
    if _T2S is None:
        try:
            from opencc import OpenCC
            _T2S = OpenCC("t2s")
        except Exception:
            _T2S = False  # 装不上就跳过，不影响主流程
    return _T2S


def to_simplified(text: str) -> str:
    conv = _get_converter()
    if not conv or not text:
        return text
    try:
        return conv.convert(text)
    except Exception:
        return text


def apply_replacements(text: str, table: dict) -> str:
    """按用户表替换。**长词优先**，否则短词会先把长词吃掉。

    举例：表里同时有「识别」→「辨识」和「识别率」→「准确率」，
    按 dict 原顺序执行时「识别率」会先被替换成「辨识率」，
    第二条规则永远命中不了。按长度降序就没有这个问题。
    """
    if not text or not table:
        return text
    for k in sorted(table, key=len, reverse=True):
        v = table[k]
        if k:
            text = text.replace(k, v)
    return text


def tidy(text: str) -> str:
    """轻度规整：合并空白、修掉重复标点。不改语义。"""
    if not text:
        return text
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"([，。！？、；：])\1+", r"\1", text)
    text = re.sub(r"\s+([，。！？、；：])", r"\1", text)
    return text


def process(text: str, cfg: dict) -> str:
    text = tidy(text)
    if cfg.get("to_simplified", True):
        text = to_simplified(text)
    text = apply_replacements(text, cfg.get("replacements", {}))
    return tidy(text)
