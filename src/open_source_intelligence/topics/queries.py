"""Read-side queries behind the topic endpoints.

`pipeline/queries.py`, `reports/queries.py` and `observability/queries.py` set the precedent this
module follows: the API and web layers call these functions and never issue SQL themselves
(`INTERFACES.md` §4). Nothing here writes a row, and nothing here needs a migration — every column
it reads already existed.

Two decisions are load-bearing:

* **The visibility rule is not re-implemented.** `ADMIN` sees every topic and everyone else sees
  the enabled ones: that is `security/authorization.py:can_read_topic`, restated as a SQL
  predicate so the filter reaches the database instead of running after the fetch. A list that
  dropped invisible rows in Python would still report a `total` counting them, which answers a
  question the caller is not allowed to ask.
* **`q` is a literal substring search**, escaped by the same helper the repository catalog uses
  (`core/text.py`). Two callers, one definition of what escaping means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from open_source_intelligence.core.text import LIKE_ESCAPE_CHAR, literal_like
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.security.authorization import readable_topics_filter
from open_source_intelligence.security.service import get_role_codes

#: The documented list contract limits `page_size` to 1-100; the same bound applies here so every
#: list endpoint in the platform refuses an unbounded page the same way.
DEFAULT_PAGE_SIZE: Final[int] = 20
MAX_PAGE_SIZE: Final[int] = 100


@dataclass(frozen=True, slots=True)
class TopicPage:
    """One page of topics plus the total matching the filters."""

    items: tuple[Topic, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """Keep a requested page size inside the documented 1-100 range."""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def _topic_conditions(
    *,
    user: User,
    roles: set[str],
    enabled: bool | None,
    query: str | None,
) -> list[ColumnElement[bool]]:
    """Build the filter list shared by the page query and its count query.

    Sharing the list is the point: a count that used a different predicate than the page would
    report a `total` the caller can never page through. Here the shared predicate is also what
    keeps invisible topics out of `total`.
    """
    conditions: list[ColumnElement[bool]] = [readable_topics_filter(user, roles)]
    if enabled is not None:
        conditions.append(Topic.enabled.is_(enabled))
    if query is not None:
        pattern = f"%{literal_like(query.strip().lower())}%"
        conditions.append(
            or_(
                func.lower(Topic.name).like(pattern, escape=LIKE_ESCAPE_CHAR),
                func.lower(func.coalesce(Topic.description, "")).like(
                    pattern, escape=LIKE_ESCAPE_CHAR
                ),
            )
        )
    return conditions


def list_topics(
    session: Session,
    *,
    user: User,
    enabled: bool | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> TopicPage:
    """Return one page of visible topics, ordered the way the daily pipeline reads them.

    The `id`-ascending order is not arbitrary: `pipeline/daily.py:list_enabled_topics` uses it, so
    "the order of the list" and "the order the pipeline runs in" are the same order rather than two
    independently reasonable ones.
    """
    roles = get_role_codes(user)
    conditions = _topic_conditions(user=user, roles=roles, enabled=enabled, query=query)
    total = session.scalar(select(func.count()).select_from(Topic).where(*conditions)) or 0
    resolved_page = max(1, page)
    resolved_size = clamp_page_size(page_size)
    rows = session.scalars(
        select(Topic)
        .where(*conditions)
        .order_by(Topic.id.asc())
        .offset((resolved_page - 1) * resolved_size)
        .limit(resolved_size)
    ).all()
    return TopicPage(
        items=tuple(rows),
        page=resolved_page,
        page_size=resolved_size,
        total=total,
    )
