"""守护词法检索模块的用例。"""

from __future__ import annotations

from open_source_intelligence.agent.retrieval.vector import (
    rank_snippets,
    rank_texts,
    split_snippets,
    tokenize,
)


def test_tokenize_splits_ascii_words_and_cjk_bigrams() -> None:
    tokens = tokenize("OpenAI released a new agent runtime")
    assert "openai" in tokens
    assert "agent" in tokens
    assert "runtime" in tokens


def test_tokenize_turns_chinese_into_bigrams() -> None:
    tokens = tokenize("开源技术情报")
    # "开源", "源技", "技术", "术情", "情报"
    assert "开源" in tokens
    assert "技术" in tokens
    assert "情报" in tokens


def test_tokenize_drops_short_tokens() -> None:
    tokens = tokenize("a b c agent")
    assert tokens == ["agent"]


def test_rank_snippets_prefers_the_relevant_snippet() -> None:
    snippets = [
        "This section explains how to configure authentication and login tokens.",
        "This section lists the benchmark results across many datasets.",
        "This section documents the release notes and version history.",
    ]
    ranked = rank_snippets("authentication login", snippets)
    assert ranked[0].text == snippets[0]
    assert ranked[0].score > ranked[1].score


def test_rank_snippets_preserves_order_on_blank_query() -> None:
    snippets = ["first snippet", "second snippet", "third snippet"]
    ranked = rank_snippets("", snippets)
    assert [item.text for item in ranked] == snippets[: len(snippets)]


def test_rank_snippets_returns_zero_when_nothing_matches() -> None:
    snippets = ["completely unrelated words", "still nothing relevant"]
    ranked = rank_snippets("authentication", snippets)
    assert ranked[0].score == 0.0


def test_rank_texts_orders_by_relevance() -> None:
    texts = ["security notes about authentication", "a cooking recipe", "authentication guide"]
    ranked = rank_texts("authentication", texts)
    assert "authentication" in ranked[0].text


def test_split_snippets_respects_max_chars() -> None:
    text = "short paragraph one.\n\nshort paragraph two."
    snippets = split_snippets(text, 30)
    assert all(len(snippet) <= 30 for snippet in snippets)
    assert len(snippets) == 2


def test_split_snippets_hard_cuts_an_oversized_paragraph() -> None:
    text = "x" * 100
    snippets = split_snippets(text, 30)
    assert all(len(snippet) <= 30 for snippet in snippets)
    assert len(snippets) == 4
