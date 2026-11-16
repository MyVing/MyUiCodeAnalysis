"""需求文本的归一化与分词：召回层唯一的文本入口。

为什么不用向量检索：首版要求可解释、可复测、零外部依赖。召回结果要能逐条
回答「命中了哪个词、来自哪个信号」，所以这里只做确定性文本处理。
"""

from __future__ import annotations

import re
import unicodedata

# 驼峰拆分：连续大写（缩写）保持成一个词，其余按大小写边界切开
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """全角转半角、统一小写、压缩空白。

    逐字引文校验（阶段 3）复用同一套归一化，保证「模型引文」与「源码原文」
    在同一把尺子上比较，否则全角括号、不间断空格这类差异会把正确引文判成错的。
    """
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def ascii_words(text: str) -> list[str]:
    """把标识符 / URL 切成小写词：`showFilter` → [show, filter]，`/api/users` → [api, users]。

    必须在 casefold **之前**切分：先转小写就丢掉驼峰边界，`showFilter` 会变成
    一个词，`filter` 便命中不了。
    """
    words: list[str] = []
    for run in _ASCII_RUN_RE.findall(unicodedata.normalize("NFKC", text)):
        words.extend(part.casefold() for part in _CAMEL_RE.findall(run))
    return words


def has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def is_ascii_only(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def contains_cjk(text: str, surface: str) -> bool:
    """中文走子串匹配；英文走分词后的整词匹配，避免 order 命中 border。"""
    if is_ascii_only(surface):
        return surface.casefold() in set(ascii_words(text))
    return normalize(surface) in normalize(text)
