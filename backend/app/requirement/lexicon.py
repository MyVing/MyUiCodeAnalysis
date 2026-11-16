"""词表与检索词扩展：把一段中文需求变成可逐条解释的检索词。

设计取舍：不引 embedding。`筛选` → `filter` 这类跨语言映射靠词表完成，未知词
靠中文 bigram 兜底。好处是每条命中都能说出「哪个词命中了哪段原文」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.requirement.text import ascii_words, contains_cjk, normalize

LEXICON_PATH = Path(__file__).with_name("lexicon.json")

# 词的来源决定它的可信度。需求原文里出现的词最可信；词表扩展出的同义词是
# 跨语言映射（`筛选` → `filter`），间接一层；兜底切出的 bigram 最弱，只用来
# 命中字面重复的文案。
LITERAL_TERM_WEIGHT = 1.0
EXPANDED_TERM_WEIGHT = 0.8
ASCII_TERM_WEIGHT = 0.7
BIGRAM_TERM_WEIGHT = 0.45

_MIN_ASCII_LEN = 2


@dataclass(frozen=True)
class SearchTerm:
    surface: str
    weight: float
    concept: str | None = None


@dataclass(frozen=True)
class RequirementTerms:
    """一次需求文本的检索词集合。"""

    terms: tuple[SearchTerm, ...]
    concepts: tuple[str, ...]

    def surfaces(self) -> list[str]:
        return [term.surface for term in self.terms]


@dataclass(frozen=True)
class _Lexicon:
    concepts: dict[str, tuple[str, ...]]
    ascii_stopwords: frozenset[str]
    zh_stopwords: frozenset[str]

    def surfaces_of(self, concept: str) -> tuple[str, ...]:
        return self.concepts[concept]


@lru_cache
def load_lexicon() -> _Lexicon:
    raw: dict[str, Any] = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    concepts = {
        name: tuple(str(surface) for surface in surfaces)
        for name, surfaces in raw["concepts"].items()
    }
    stopwords = raw["stopwords"]
    return _Lexicon(
        concepts=concepts,
        ascii_stopwords=frozenset(str(word).casefold() for word in stopwords["en"]),
        zh_stopwords=frozenset(str(word) for word in stopwords["zh"]),
    )


def expand(requirement: str) -> RequirementTerms:
    """需求文本 → 去重后的检索词，权重按来源分档。"""
    lexicon = load_lexicon()
    norm = normalize(requirement)

    # 命中的概念 → 需求里实际出现的表面形式（只用于判断「原文提到过」）
    hit_concepts: dict[str, tuple[str, ...]] = {}
    for concept, surfaces in lexicon.concepts.items():
        matched = tuple(surface for surface in surfaces if contains_cjk(norm, surface))
        if matched:
            hit_concepts[concept] = matched

    # 需求原文里出现的词（含词表命中的中文词）置信度最高
    literal_surfaces: dict[str, str] = {}
    for concept, matched in hit_concepts.items():
        for surface in matched:
            literal_surfaces.setdefault(surface, concept)

    collected: dict[str, SearchTerm] = {}
    for surface, concept in literal_surfaces.items():
        _add(collected, SearchTerm(surface=surface, weight=LITERAL_TERM_WEIGHT, concept=concept))

    # 命中的概念 → 展开成该概念的全表面形式，完成中文需求到英文标识符的映射
    for concept in hit_concepts:
        for surface in lexicon.surfaces_of(concept):
            _add(
                collected,
                SearchTerm(surface=surface, weight=EXPANDED_TERM_WEIGHT, concept=concept),
            )

    # 需求里出现但词表没覆盖的英文词：直接当检索词，能命中英文文案与标识符
    for word in ascii_words(norm):
        if len(word) < _MIN_ASCII_LEN or word in lexicon.ascii_stopwords:
            continue
        _add(collected, SearchTerm(surface=word, weight=ASCII_TERM_WEIGHT))

    # 需求里出现但词表没覆盖的中文：bigram 兜底，只命中字面相同的文案
    for run in _cjk_runs(norm):
        for bigram in _uncovered_bigrams(run, list(literal_surfaces)):
            if bigram in lexicon.zh_stopwords:
                continue
            _add(collected, SearchTerm(surface=bigram, weight=BIGRAM_TERM_WEIGHT))

    ordered = tuple(sorted(collected.values(), key=lambda term: (-term.weight, term.surface)))
    return RequirementTerms(terms=ordered, concepts=tuple(sorted(hit_concepts)))


def _add(collected: dict[str, SearchTerm], term: SearchTerm) -> None:
    """同一表面形式只保留权重最高的那条，避免 `状态` 既算词表又算 bigram 双重计分。"""
    existing = collected.get(term.surface)
    if existing is None or term.weight > existing.weight:
        collected[term.surface] = term


def _is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def _cjk_runs(text: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in text:
        if "一" <= char <= "鿿":
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _uncovered_bigrams(run: str, known_surfaces: list[str]) -> list[str]:
    """只对词表没覆盖的连续片段切 bigram。

    不先挖掉已知词，"增加状态筛选" 会切出 "加状"、"态筛" 这类跨词边界的噪声，
    它们几乎不可能命中，却会污染 signals 的可读性。
    """
    covered = [False] * len(run)
    for surface in known_surfaces:
        start = run.find(surface)
        while start != -1:
            for index in range(start, start + len(surface)):
                covered[index] = True
            start = run.find(surface, start + 1)

    segments: list[str] = []
    start_index = 0
    while start_index < len(run):
        if covered[start_index]:
            start_index += 1
            continue
        end_index = start_index
        while end_index < len(run) and not covered[end_index]:
            end_index += 1
        segments.append(run[start_index:end_index])
        start_index = end_index

    bigrams: list[str] = []
    for segment in segments:
        bigrams.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return bigrams
