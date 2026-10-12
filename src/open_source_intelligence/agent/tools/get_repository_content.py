"""`get_repository_content` implementation: cleaned and truncated README text."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from .truncation import clean_untrusted_text, content_digest, truncate_text


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
    content, truncated = truncate_text(cleaned, payload.max_chars)
    snippet = ContentSnippet(
        repo_id=payload.repo_id,
        source_url=record.source_url,
        content=content,
        content_hash=content_digest(content),
        truncated=truncated,
        untrusted=True,
    )
    return ToolOutcome(data=snippet, result_count=1, truncated=truncated)
