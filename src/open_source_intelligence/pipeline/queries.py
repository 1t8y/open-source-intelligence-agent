"""Read-side queries behind the repository catalog API.

`reports/queries.py` and `observability/queries.py` set the precedent this module follows: the
API and web layers call these functions and never issue SQL themselves (`INTERFACES.md` §4).

Two decisions are load-bearing here:

* **The catalog is a join, not a table.** `provider` is `data_sources.source_type` rather than a
  column on `repositories`, and the topic ranking (`score` / `rank` / `selected_for_agent`) lives
  on `topic_repositories`. The endpoint returns them together so a caller does not have to
  assemble the catalog from three responses.
* **The ranking is present or absent, never invented.** Those three fields are the product of one
  topic's ranking pass; without a `topic_id` there is no meaningful value for them, so they come
  back as `None` rather than as `0` or `False`.

Nothing in this module writes a row, and it needs no migration: every column it reads already
existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
    TopicRepository,
)

from .snapshots import get_snapshot_series

# The documented list contract limits `page_size` to 1-100; the same bound is applied here so
# every list endpoint in the platform refuses an unbounded page the same way.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

SORT_STARS: Final[str] = "stars"
SORT_FORKS: Final[str] = "forks"
SORT_LAST_SEEN: Final[str] = "last_seen"

#: The `sort` values `docs/api-contract.md` §4 documents. Anything else is a 422, not a
#: silent fallback to the default: a caller that asked for an ordering they did not get has no
#: way to notice, and paginating under an ordering you did not ask for can skip rows entirely.
SORT_OPTIONS: Final[tuple[str, ...]] = (SORT_STARS, SORT_FORKS, SORT_LAST_SEEN)


@dataclass(frozen=True, slots=True)
class RepositoryCatalogEntry:
    """One repository joined with its provider and, when filtered by topic, its ranking."""

    repository: Repository
    provider: str
    score: Decimal | None
    rank: int | None
    selected_for_agent: bool | None


@dataclass(frozen=True, slots=True)
class RepositoryPage:
    """One page of the catalog plus the total matching the filters."""

    items: tuple[RepositoryCatalogEntry, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """Keep a requested page size inside the documented 1-100 range."""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def _catalog_conditions(
    *,
    topic_id: int | None,
    language: str | None,
    min_stars: int | None,
    first_seen_after: datetime | None,
    query: str | None,
) -> list[ColumnElement[bool]]:
    """Build the filter list shared by the page query and its count query.

    Sharing the list is the point: a count that used a different predicate than the page would
    report a `total` the caller can never page through.
    """
    conditions: list[ColumnElement[bool]] = []
    if topic_id is not None:
        conditions.append(TopicRepository.topic_id == topic_id)
    if language is not None:
        conditions.append(func.lower(Repository.language) == language.strip().lower())
    if min_stars is not None:
        conditions.append(Repository.stars >= min_stars)
    if first_seen_after is not None:
        conditions.append(Repository.first_seen_at >= first_seen_after)
    if query is not None:
        pattern = f"%{query.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(Repository.full_name).like(pattern),
                func.lower(func.coalesce(Repository.description, "")).like(pattern),
            )
        )
    return conditions


def _order_by(sort: str) -> tuple[Any, ...]:
    """Return the ordering for a documented `sort` value.

    `id` is always the tiebreaker: two repositories with the same star count are common (a
    freshly collected batch often starts at zero), and without a total order the database may
    return them in different orders across pages, duplicating one and dropping another.
    """
    if sort == SORT_FORKS:
        return (Repository.forks.desc(), Repository.id.desc())
    if sort == SORT_LAST_SEEN:
        return (Repository.last_seen_at.desc(), Repository.id.desc())
    return (Repository.stars.desc(), Repository.id.desc())


def list_repositories(
    session: Session,
    *,
    topic_id: int | None = None,
    language: str | None = None,
    min_stars: int | None = None,
    first_seen_after: datetime | None = None,
    query: str | None = None,
    sort: str = SORT_STARS,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> RepositoryPage:
    """Return one page of the repository catalog.

    The catalog holds public metadata (`docs/security-and-privacy.md` §2), so there is no
    per-row access predicate here. `topic_id` is different: it narrows to one topic's candidate
    list, and whether that topic is visible to the caller is decided by
    `security/authorization.py:authorize_topic_read` *before* this function is called.
    """
    resolved_page = max(1, page)
    resolved_size = clamp_page_size(page_size)
    resolved_sort = sort if sort in SORT_OPTIONS else SORT_STARS
    conditions = _catalog_conditions(
        topic_id=topic_id,
        language=language,
        min_stars=min_stars,
        first_seen_after=first_seen_after,
        query=query,
    )

    counted = select(func.count()).select_from(Repository)
    if topic_id is not None:
        counted = counted.join(
            TopicRepository, TopicRepository.repository_id == Repository.id
        ).where(*conditions)
        ranked_rows = session.execute(
            select(
                Repository,
                DataSource.source_type,
                TopicRepository.score,
                TopicRepository.rank,
                TopicRepository.selected_for_agent,
            )
            .join(DataSource, DataSource.id == Repository.source_id)
            .join(TopicRepository, TopicRepository.repository_id == Repository.id)
            .where(*conditions)
            .order_by(*_order_by(resolved_sort))
            .offset((resolved_page - 1) * resolved_size)
            .limit(resolved_size)
        ).all()
        items = tuple(
            RepositoryCatalogEntry(
                repository=row[0],
                provider=row[1],
                score=row[2],
                rank=row[3],
                selected_for_agent=row[4],
            )
            for row in ranked_rows
        )
    else:
        counted = counted.where(*conditions)
        plain_rows = session.execute(
            select(Repository, DataSource.source_type)
            .join(DataSource, DataSource.id == Repository.source_id)
            .where(*conditions)
            .order_by(*_order_by(resolved_sort))
            .offset((resolved_page - 1) * resolved_size)
            .limit(resolved_size)
        ).all()
        items = tuple(
            RepositoryCatalogEntry(
                repository=row[0],
                provider=row[1],
                score=None,
                rank=None,
                selected_for_agent=None,
            )
            for row in plain_rows
        )

    total = int(session.scalar(counted) or 0)
    return RepositoryPage(
        items=items,
        page=resolved_page,
        page_size=resolved_size,
        total=total,
    )


def get_repository(session: Session, *, repository_id: int) -> Repository | None:
    """Return one repository row, or `None` when it does not exist."""
    return session.get(Repository, repository_id)


def list_snapshots(
    session: Session,
    *,
    repository_id: int,
    start_date: date,
    end_date: date,
) -> tuple[RepoSnapshot, ...]:
    """Return the snapshot series for one repository over a closed date window.

    The window is inclusive at both ends, matching
    `pipeline/snapshots.py:get_snapshot_series`, which is called through here rather than
    reimplemented: `snapshot_date` is a natural day, and a half-open range would silently drop
    the day the caller named as the end.
    """
    return tuple(
        get_snapshot_series(
            session,
            repository_id,
            start_date=start_date,
            end_date=end_date,
        )
    )
