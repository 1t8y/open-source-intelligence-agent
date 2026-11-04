"""`get_repository_profile` 的实现：主数据与最新计数。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import DataSource

from .access import load_authorized_repository
from .context import ToolContext
from .contracts import GetRepositoryProfileInput, RepositoryProfile
from .outcome import ToolOutcome


def get_repository_profile(
    session: Session,
    ctx: ToolContext,
    payload: GetRepositoryProfileInput,
) -> ToolOutcome:
    """返回仓库画像，全程不暴露任何凭据。"""
    repository = load_authorized_repository(session, ctx, payload.repo_id)
    source = session.get(DataSource, repository.source_id)
    profile = RepositoryProfile(
        repo_id=repository.id,
        provider=source.source_type if source is not None else "unknown",
        full_name=repository.full_name,
        html_url=repository.html_url,
        description=repository.description,
        language=repository.language,
        license=repository.license,
        topics=[str(topic) for topic in (repository.topics_json or [])],
        stars=repository.stars,
        forks=repository.forks,
        first_seen_at=repository.first_seen_at,
        last_seen_at=repository.last_seen_at,
    )
    return ToolOutcome(data=profile, result_count=1, truncated=False)
