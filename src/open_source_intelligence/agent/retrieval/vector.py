"""Lightweight lexical retrieval: term-frequency vectors and cosine similarity.

The RAG layer needs a way to pick the *most relevant* slice of a long README, and to rank prior
reports by relevance instead of by recency, without pulling in a model runtime or a vector
database. This module does that with a deliberately small, dependency-free bag-of-words model:

* Text is tokenized into a mix of ASCII word tokens and CJK character bigrams, so an English
  README and a Chinese report are both searchable.
* Each snippet and the query are turned into TF-IDF vectors over a shared vocabulary, and ranked
  by cosine similarity. "Relevant" means "shares the same distinctive terms", which is exactly
  what the two call sites need: a section about "authentication" should outrank one about
  "benchmarks" for a query about login.

This is lexical, not semantic: it will not connect "login" to "sign-in". That is the deliberate
cost of staying dependency-free and offline. A real embedding model is a drop-in upgrade at the
two call sites, which never see the vectors.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: ASCII word token (letters, digits, and the common code-symbol runs like `openai` / `v2`).
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]*")
#: A run of CJK characters, split into bigrams by the tokenizer.
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")

_MIN_TERM_LENGTH = 2
#: Number of most-relevant snippets to return when none is requested.
DEFAULT_TOP_K = 3


@dataclass(frozen=True, slots=True)
class RankedSnippet:
    """One ranked slice of source text plus its similarity to the query.

    `index` is the position the text had in the input sequence. Callers need it to map a ranked
    result back to the row it came from: two documents can share the same text (two reports with
    the same title and the same opening paragraph), and a text-keyed lookup would then return one
    row twice and drop the other.
    """

    text: str
    score: float
    index: int = 0


def _cjk_bigrams(run: str) -> list[str]:
    """Split one contiguous CJK run into overlapping character bigrams.

    A single character yields itself, so a one-character query still matches. Bigrams (rather
    than single characters) give Chinese text enough term discrimination to matter.
    """
    if len(run) == 1:
        return [run]
    return [run[index : index + 2] for index in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Return the normalized term tokens of `text`.

    ASCII runs become lowercased word tokens; CJK runs become character bigrams. Everything
    else (punctuation, whitespace) is a separator, so a URL or a code symbol like `repo_id`
    contributes real terms without a separate parser.
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
    """Return raw term counts for one document."""
    return Counter(tokens)


def _idf(term: str, document_frequencies: Counter[str], document_count: int) -> float:
    """Return the inverse document frequency of one term.

    Terms that appear in almost every snippet (like "the" or a project name) get down-weighted;
    a term unique to one snippet gets a full weight.
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
    """Return the TF-IDF vector of one document as a sparse term -> weight mapping."""
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
    """Return the L2 norm of a sparse vector."""
    return math.sqrt(sum(weight * weight for weight in vector.values()))


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Return the cosine similarity of two sparse vectors."""
    if not left or not right:
        return 0.0
    left_norm = _norm(left)
    right_norm = _norm(right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    return dot / (left_norm * right_norm)


def split_snippets(text: str, max_chars: int) -> list[str]:
    """Split `text` into snippets of at most `max_chars`, preferring paragraph boundaries.

    A README is usually already structured into sections by blank lines or Markdown headings;
    cutting on those keeps a snippet self-contained instead of severing a sentence in the middle.
    Oversized paragraphs are hard-cut so the guarantee on `max_chars` always holds.
    """
    if not text.strip():
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    snippets: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            snippets.append(paragraph)
            continue
        # Hard-split an oversized paragraph; the contract is a bound, not a sentence.
        for index in range(0, len(paragraph), max_chars):
            snippets.append(paragraph[index : index + max_chars])
    return snippets


def rank_snippets(
    query: str,
    snippets: Sequence[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[RankedSnippet]:
    """Rank `snippets` by relevance to `query` and return the top `top_k`.

    When the query is blank there is nothing to rank by, so the snippets keep their original
    order. When no snippet shares any term with the query, every score is zero and the original
    order is preserved too — a deterministic fallback beats a random shuffle.
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
    """Rank arbitrary texts (e.g. prior-report summaries) by relevance to `query`.

    A convenience wrapper over `rank_snippets` for callers that already have whole documents
    rather than pre-split snippets.
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
