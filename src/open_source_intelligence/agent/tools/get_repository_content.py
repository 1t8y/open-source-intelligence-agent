"""`get_repository_content` implementation: cleaned, query-ranked, and size-bounded README text.

When the caller supplies a `query_hint`, a long README is no longer hard-truncated at its head.
Instead the README is split into self-contained snippets and the ones most relevant to the query
are returned, so a section buried deep in a 20k-character document (say, the security notes) is
not silently dropped. The query ranking is lexical (see `agent/retrieval/vector.py`), stays
offline, and falls back to head truncation whenever there is no hint.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.retrieval.vector import rank_snippets, split_snippets
from open_source_intelligence.core.text import (
    clean_untrusted_text,
    content_digest,
    truncate_text,
)
from open_source_intelligence.persistence.models.intelligence import RepositoryContent

from .access import load_authorized_repository
from .context import ToolContext
from .contracts import (
    TOOL_NOT_FOUND,
    ContentSnippet,
    GetRepositoryContentInput,
)
from .errors import ToolExecutionError
from .outcome import ToolOutcome


def get_repository_content(
    session: Session,
    ctx: ToolContext,
    payload: GetRepositoryContentInput,
) -> ToolOutcome:
    """Return cached content, always flagged as untrusted and size-bounded."""
    load_authorized_repository(session, ctx, payload.repo_id)
    record = session.scalar(
        select(RepositoryContent).where(
            RepositoryContent.repository_id == payload.repo_id,
            RepositoryContent.content_type == payload.content_type,
        )
    )
    if record is None:
        raise ToolExecutionError(TOOL_NOT_FOUND, "repository content not available")
    cleaned = clean_untrusted_text(record.content)
    content, truncated = _select_content(cleaned, payload.max_chars, payload.query_hint)
    snippet = ContentSnippet(
        repo_id=payload.repo_id,
        source_url=record.source_url,
        content=content,
        content_hash=content_digest(content),
        truncated=truncated,
        untrusted=True,
    )
    return ToolOutcome(data=snippet, result_count=1, truncated=truncated)


def _select_content(cleaned: str, max_chars: int, query_hint: str | None) -> tuple[str, bool]:
    """Return content capped at `max_chars`, ranking snippets by `query_hint` when given.

    With no hint (or content already within budget) the behaviour is unchanged: take the head
    and report whether anything was cut. With a hint, the most relevant snippets are concatenated
    until `max_chars` is reached, so relevance — not position — decides what survives truncation.
    """
    if len(cleaned) <= max_chars:
        return cleaned, False
    if not query_hint or not query_hint.strip():
        return truncate_text(cleaned, max_chars)

    snippets = split_snippets(cleaned, max_chars)
    ranked = rank_snippets(query_hint, snippets, top_k=len(snippets))
    selected: list[str] = []
    used = 0
    for item in ranked:
        if used + len(item.text) > max_chars:
            continue
        selected.append(item.text)
        used += len(item.text)
    if not selected:
        # Every snippet alone exceeds the budget (or nothing matched): fall back to the head so
        # the tool still returns something deterministic rather than an empty document.
        return truncate_text(cleaned, max_chars)
    return "\n\n".join(selected), True
