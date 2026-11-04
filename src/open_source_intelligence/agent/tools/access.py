"""只读工具共用的主题范围授权检查。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import Repository, TopicRepository

from .context import ToolContext
from .contracts import TOOL_ACCESS_DENIED, TOOL_NOT_FOUND
from .errors import ToolExecutionError


def repository_is_authorized(session: Session, ctx: ToolContext, repo_id: int) -> bool:
    """判断仓库是否归属于某个已授权主题。"""
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
    """加载仓库；失败时按契约错误码把错误抛给调用方。"""
    repository = session.get(Repository, repo_id)
    if repository is None:
        raise ToolExecutionError(TOOL_NOT_FOUND, "repository not found")
    if not repository_is_authorized(session, ctx, repo_id):
        raise ToolExecutionError(
            TOOL_ACCESS_DENIED,
            "repository is outside the authorized topic scope",
        )
    return repository
