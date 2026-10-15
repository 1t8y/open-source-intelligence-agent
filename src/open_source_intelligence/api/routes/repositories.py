"""Repository catalog endpoints: the shared repository list and one repository's trend.

`docs/api-contract.md` §4 fixes what these two routes answer. Two properties are worth stating
in code because they are easy to get wrong later:

* **The catalog is public.** `docs/security-and-privacy.md` §2 classifies public repository
  metadata as public, so there is no per-row access predicate here and every authenticated role
  reads the same list. The only thing that needs adjudicating is which topic the list may be
  sliced by, and that decision comes from `security.authorization.authorize_topic_read`.
* **The window is a closed date range.** `pipeline/queries.list_snapshots` delegates to
  `get_snapshot_series`, so this route does not have its own opinion about where the range
  starts and stops.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.pipeline.queries import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RepositoryCatalogEntry,
    get_repository,
    list_repositories,
    list_snapshots,
)
from open_source_intelligence.security.authorization import (
    REPOSITORY_READ_ROLES,
    authorize_topic_read,
)
from open_source_intelligence.security.dependencies import get_db_session, require_roles

from ..params import UtcDateTime

router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])

CatalogUser = Annotated[User, Depends(require_roles(*REPOSITORY_READ_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

UnprocessableEntity = status.HTTP_422_UNPROCESSABLE_CONTENT

#: How far back the trend route looks when the caller names no window. Thirty days covers the
#: weekly report's seven-day window several times over without letting a default turn into an
#: unbounded scan of a table that grows one row per repository per day.
DEFAULT_SNAPSHOT_WINDOW_DAYS = 30

#: The longest window the trend route will serve. The bound is checked rather than clamped: a
#: clamped answer is silently about a different period than the one that was asked for, and
#: both numbers look plausible, so the mistake is invisible.
MAX_SNAPSHOT_WINDOW_DAYS = 366


class RepositorySummaryResponse(BaseModel):
    """One catalog row: the repository plus its provider and, when sliced by topic, its rank."""

    id: int
    source_id: int
    provider: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    license: str | None
    topics: list[str] | None
    stars: int
    forks: int
    first_seen_at: str
    last_seen_at: str
    score: str | None
    rank: int | None
    selected_for_agent: bool | None


class RepositoryListResponse(BaseModel):
    """One page of the repository catalog."""

    items: list[RepositorySummaryResponse]
    page: int
    page_size: int
    total: int


class SnapshotResponse(BaseModel):
    """One day of a repository's trend."""

    snapshot_date: str
    stars: int
    forks: int
    open_issues: int | None
    stars_delta: int | None
    forks_delta: int | None


class SnapshotSeriesResponse(BaseModel):
    """A repository's snapshot series over the resolved closed window."""

    repository_id: int
    provider: str
    full_name: str
    start_date: str
    end_date: str
    granularity: str
    items: list[SnapshotResponse]


def _summary(entry: RepositoryCatalogEntry) -> RepositorySummaryResponse:
    """Serialize one catalog entry.

    `score` leaves as text for the same reason `estimated_cost` does in the Agent ledger: it is
    a `NUMERIC(12,6)` value, and rendering it as a JSON float would let the value that comes back
    differ from the value that was stored.
    """
    repository = entry.repository
    return RepositorySummaryResponse(
        id=repository.id,
        source_id=repository.source_id,
        provider=entry.provider,
        full_name=repository.full_name,
        html_url=repository.html_url,
        description=repository.description,
        language=repository.language,
        license=repository.license,
        topics=list(repository.topics_json) if repository.topics_json else None,
        stars=repository.stars,
        forks=repository.forks,
        first_seen_at=repository.first_seen_at.isoformat(),
        last_seen_at=repository.last_seen_at.isoformat(),
        score=None if entry.score is None else str(entry.score),
        rank=entry.rank,
        selected_for_agent=entry.selected_for_agent,
    )


@router.get("", response_model=RepositoryListResponse)
def list_repository_catalog(
    user: CatalogUser,
    session: DbSession,
    request: Request,
    topic_id: Annotated[int | None, Query(ge=1)] = None,
    language: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    min_stars: Annotated[int | None, Query(ge=0)] = None,
    created_after: Annotated[UtcDateTime | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    sort: Annotated[Literal["stars", "forks", "last_seen"], Query()] = "stars",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> RepositoryListResponse:
    """Return one page of the repository catalog.

    `topic_id` is the only filter that carries an authorization decision: it narrows the list to
    one topic's candidates, and a topic the caller may not see is refused before the query runs.
    """
    if topic_id is not None:
        authorize_topic_read(session, request, user, topic_id=topic_id)
    page_result = list_repositories(
        session,
        topic_id=topic_id,
        language=language,
        min_stars=min_stars,
        first_seen_after=created_after,
        query=q,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return RepositoryListResponse(
        items=[_summary(entry) for entry in page_result.items],
        page=page_result.page,
        page_size=page_result.page_size,
        total=page_result.total,
    )


@router.get("/{repository_id}/snapshots", response_model=SnapshotSeriesResponse)
def get_repository_snapshots(
    user: CatalogUser,
    session: DbSession,
    now: Annotated[datetime, Depends(utc_now)],
    repository_id: Annotated[int, Path(ge=1)],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    granularity: Annotated[Literal["day"], Query()] = "day",
) -> SnapshotSeriesResponse:
    """Return a repository's snapshot series over a closed date window.

    An empty window is a `200` with an empty list, not a `404`: the repository exists and the
    honest answer is "it has no snapshots in this range". A repository that does not exist is the
    `404`.
    """
    resolved_end = end or now.date()
    resolved_start = start or resolved_end - timedelta(days=DEFAULT_SNAPSHOT_WINDOW_DAYS)
    if resolved_start > resolved_end:
        raise HTTPException(
            status_code=UnprocessableEntity,
            detail="start must not be later than end",
        )
    if (resolved_end - resolved_start).days > MAX_SNAPSHOT_WINDOW_DAYS:
        raise HTTPException(
            status_code=UnprocessableEntity,
            detail=f"window must not exceed {MAX_SNAPSHOT_WINDOW_DAYS} days",
        )

    repository = get_repository(session, repository_id=repository_id)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"repository {repository_id} does not exist",
        )
    snapshots = list_snapshots(
        session,
        repository_id=repository_id,
        start_date=resolved_start,
        end_date=resolved_end,
    )
    return SnapshotSeriesResponse(
        repository_id=repository.id,
        provider=repository.source.source_type,
        full_name=repository.full_name,
        start_date=resolved_start.isoformat(),
        end_date=resolved_end.isoformat(),
        granularity=granularity,
        items=[
            SnapshotResponse(
                snapshot_date=snapshot.snapshot_date.isoformat(),
                stars=snapshot.stars,
                forks=snapshot.forks,
                open_issues=snapshot.open_issues,
                stars_delta=snapshot.stars_delta,
                forks_delta=snapshot.forks_delta,
            )
            for snapshot in snapshots
        ],
    )
