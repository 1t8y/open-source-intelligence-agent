"""`get_repository_content` 的实现：清洗、按查询排序并限长的 README 文本。

当调用方给了 `query_hint`，长 README 不再在开头被硬截断，而是切成自包含的
片段，返回与查询最相关的那些：埋在 2 万字符文档深处的章节（比如安全说明）
不会被悄悄丢掉。查询排序是词法级的（见 `agent/retrieval/vector.py`），
全程离线；没有提示时回退到截取开头。
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
    """返回缓存内容，始终标记为不可信并受长度上限约束。"""
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
    """返回不超过 `max_chars` 的内容；给了 `query_hint` 时按它给片段排序。

    没有提示（或内容本就在预算之内）时行为不变：直接取开头，并报告是否发生过
    截断。有提示时，把最相关的片段依次拼接，直到达到 `max_chars` —— 因此决定
    谁能活过截断的是相关性，而不是位置。
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
        # 每个片段单独都超出预算（或者没有任何匹配）：回退到截取开头，让工具
        # 仍能返回确定的结果，而不是一份空文档。
        return truncate_text(cleaned, max_chars)
    return "\n\n".join(selected), True
