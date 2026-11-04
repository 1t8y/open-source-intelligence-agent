"""轻量词法检索：词频向量与余弦相似度。

RAG 层需要一种方式，从一份长 README 里挑出*最相关*的一段，并按相关性而非新鲜度为
历史报告排序，同时又不想引入模型运行时或向量数据库。本模块用一个刻意
做小的、零依赖的词袋模型做到这点：

* 文本切成 ASCII 单词词元与中文字符二元组的混合，因此英文 README 与中文报告
  都能被检索。
* 片段与查询都转成同一份共享词表上的 TF-IDF 向量，再按余弦相似度排序。「相关」
  意思是「共享同样的特征词」，这正是两个调用点需要的：查询登录时，讲「认证」
  的一节应排在讲「基准测试」的一节前面。

这是词法匹配而非语义匹配：它不会把「login」与「sign-in」联系起来。这是
坚持零依赖、纯离线的刻意代价。真嵌入模型可以在这两个调用点直接替换进来，而
它们从来看不到向量本身。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: ASCII 单词词元（字母、数字，以及 `openai` / `v2` 这类常见代码符号串）。
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]*")
#: 一段连续的 CJK 字符，由分词器切成二元组。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")

_MIN_TERM_LENGTH = 2
#: 未指定时返回的最相关片段数量。
DEFAULT_TOP_K = 3


@dataclass(frozen=True, slots=True)
class RankedSnippet:
    """一段排好序的源文本，以及它与查询的相似度。

    `index` 是这段文本在输入序列中的位置。调用方需要它把排序结果映射回
    原来的行：两份文档可能文本完全相同（两篇标题与开头段落都一样的报告），
    此时以文本为键的查找会把同一行返回两次，并丢掉另一行。
    """

    text: str
    score: float
    index: int = 0


def _cjk_bigrams(run: str) -> list[str]:
    """把一段连续的 CJK 切成交叠的字符二元组。

    单个字符会返回它自己，这样单字查询仍能命中。选二元组（而不是单字）是为了给
    中文文本足够的词项区分度。
    """
    if len(run) == 1:
        return [run]
    return [run[index : index + 2] for index in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """返回 `text` 归一化后的词项。

    ASCII 串变成小写单词词元；CJK 串变成字符二元组。其余字符（标点、空白）都是
    分隔符，因此 URL 或 `repo_id` 这类代码符号无需单独的解析器就能贡献
    真实的词项。
    """
    tokens: list[str] = []
    for cjk_run in _CJK_RUN_RE.split(text):
        if not cjk_run:
            continue
        for word in _WORD_RE.findall(cjk_run):
            lowered = word.lower()
            if len(lowered) >= _MIN_TERM_LENGTH:
                tokens.append(lowered)
    for match in _CJK_RUN_RE.finditer(text):
        tokens.extend(_cjk_bigrams(match.group(0)))
    return tokens


def _term_frequency(tokens: Sequence[str]) -> Counter[str]:
    """返回一份文档的原始词频计数。"""
    return Counter(tokens)


def _idf(term: str, document_frequencies: Counter[str], document_count: int) -> float:
    """返回某个词项的反文档频率。

    几乎出现在每个片段里的词（如 "the" 或项目名）会被降权；某个片段
    独有的词则拿到全权重。
    """
    occurrences = document_frequencies.get(term, 0)
    if occurrences == 0:
        return 0.0
    return math.log((document_count + 1) / (occurrences + 1)) + 1.0


def _tfidf_vector(
    tokens: Sequence[str],
    document_frequencies: Counter[str],
    document_count: int,
) -> dict[str, float]:
    """返回一份文档的 TF-IDF 向量，即「词项 -> 权重」的稀疏映射。"""
    frequencies = _term_frequency(tokens)
    if not frequencies:
        return {}
    length = len(tokens)
    vector: dict[str, float] = {}
    for term, count in frequencies.items():
        weight = _idf(term, document_frequencies, document_count)
        if weight == 0.0:
            continue
        vector[term] = (count / length) * weight
    return vector


def _norm(vector: dict[str, float]) -> float:
    """返回稀疏向量的 L2 范数。"""
    return math.sqrt(sum(weight * weight for weight in vector.values()))


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """返回两个稀疏向量的余弦相似度。"""
    if not left or not right:
        return 0.0
    left_norm = _norm(left)
    right_norm = _norm(right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    return dot / (left_norm * right_norm)


def split_snippets(text: str, max_chars: int) -> list[str]:
    """把 `text` 切成不超过 `max_chars` 的片段，优先在段落边界处切。

    README 通常已经由空行或 Markdown 标题分好小节；沿这些边界切能让
    片段保持自足，而不是从句子中间截断。超长段落会被硬切，因此
    `max_chars` 的上界保证始终成立。
    """
    if not text.strip():
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    snippets: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            snippets.append(paragraph)
            continue
        # 硬切超长段落；契约约束的是长度上界，而不是句子完整性。
        for index in range(0, len(paragraph), max_chars):
            snippets.append(paragraph[index : index + max_chars])
    return snippets


def rank_snippets(
    query: str,
    snippets: Sequence[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[RankedSnippet]:
    """按与 `query` 的相关性给 `snippets` 排序，返回前 `top_k` 条。

    查询为空时无从排序，片段保持原有顺序。当没有任何片段与查询共享词项时，所有
    得分都是零，同样保留原有顺序 —— 确定性的兜底胜过随机打乱。
    """
    if not snippets:
        return []
    if not query.strip():
        return [
            RankedSnippet(text=snippet, score=1.0, index=index)
            for index, snippet in enumerate(snippets[:top_k])
        ]
    query_tokens = tokenize(query)
    if not query_tokens:
        return [
            RankedSnippet(text=snippet, score=0.0, index=index)
            for index, snippet in enumerate(snippets[:top_k])
        ]

    document_tokens = [tokenize(snippet) for snippet in snippets]
    document_frequencies: Counter[str] = Counter()
    for tokens in document_tokens:
        document_frequencies.update(set(tokens))
    document_count = len(snippets)

    query_vector = _tfidf_vector(query_tokens, document_frequencies, document_count)
    if not query_vector:
        return [
            RankedSnippet(text=snippet, score=0.0, index=index)
            for index, snippet in enumerate(snippets[:top_k])
        ]

    scored: list[RankedSnippet] = []
    for index, (snippet, tokens) in enumerate(zip(snippets, document_tokens, strict=True)):
        vector = _tfidf_vector(tokens, document_frequencies, document_count)
        scored.append(RankedSnippet(text=snippet, score=_cosine(query_vector, vector), index=index))
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:top_k]


def rank_texts(
    query: str,
    texts: Iterable[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[RankedSnippet]:
    """按与 `query` 的相关性给任意文本（例如历史报告摘要）排序。

    这是 `rank_snippets` 的便捷封装，供手上已是一整份文档、而非预先切
    好片段的调用方使用。
    """
    return rank_snippets(query, list(texts), top_k=top_k)


__all__ = [
    "DEFAULT_TOP_K",
    "RankedSnippet",
    "rank_snippets",
    "rank_texts",
    "split_snippets",
    "tokenize",
]
