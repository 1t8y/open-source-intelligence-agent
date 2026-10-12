"""Topic-scope authorization checks shared by the read-only tools."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import Repository, TopicRepository

from .context import ToolContext
from .contracts import TOOL_ACCESS_DENIED, TOOL_NOT_FOUND
from .errors import ToolExecutionError


def repository_is_authorized(session: Session, ctx: ToolContext, repo_id: int) -> bool:
    """Return whether the repository belongs to an authorized topic."""
    if not ctx.authorized_topic_ids:
        return False
    stmt = (
        select(TopicRepository.topic_id)
        .where(
            TopicRepository.repository_id == repo_id,
            TopicRepository.topic_id.in_(ctx.authorized_topic_ids),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def load_authorized_repository(session: Session, ctx: ToolContext, repo_id: int) -> Repository:
    """Load a repository or fail with the contract error code for the caller."""
    repository = session.get(Repository, repo_id)
    if repository is None:
        raise ToolExecutionError(TOOL_NOT_FOUND, "repository not found")
    if not repository_is_authorized(session, ctx, repo_id):
        raise ToolExecutionError(
            TOOL_ACCESS_DENIED,
            "repository is outside the authorized topic scope",
        )
    return repository
