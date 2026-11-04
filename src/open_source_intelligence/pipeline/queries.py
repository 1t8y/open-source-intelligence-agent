"""提供仓库目录 API 背后的读侧查询。

`reports/queries.py` 与 `observability/queries.py` 立下了本模块遵循的先例：API 层与 Web 层
调用这些函数，自己从不写 SQL（`INTERFACES.md` §4）。

这里有两个承重的决策：

* **目录是一次 join，而不是一张表。** `provider` 取自 `data_sources.source_type`，而不是
  `repositories` 上的某一列；主题排名（`score` / `rank` / `selected_for_agent`）则住在
  `topic_repositories` 上。端点把它们一起返回，调用方就不必从三个响应里拼装目录。
* **排名要么存在，要么缺席，绝不臆造。** 这三个字段是某个主题一次排序的产物；没有
  `topic_id` 时它们没有有意义的值，因此返回 `None`，而不是 `0` 或 `False`。

第三条规则虽小，却用户可见：`q` 按文档是子串搜索，所以调用方的 `%` 与 `_` 在进入 `LIKE`
之前会先被转义。不转义时，`q=_` 会匹配每一行。转义本身住在 `core/text.py:literal_like`，
因为主题列表对 `name` 与 `description` 施加同一条规则 —— 一份定义，两个调用方。

本模块不写任何行，也不需要迁移：它读取的每一列原本就已存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from open_source_intelligence.core.text import LIKE_ESCAPE_CHAR, literal_like
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
    TopicRepository,
)

from .snapshots import get_snapshot_series

# 有文档的列表契约把 `page_size` 限定在 1-100；这里施加同一上限，让平台上的每个列表端点
# 都以同样的方式拒绝无界分页。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

SORT_STARS: Final[str] = "stars"
SORT_FORKS: Final[str] = "forks"
SORT_LAST_SEEN: Final[str] = "last_seen"

#: `docs/api-contract.md` §4 记载的 `sort` 取值。其它任何值都是 422，而不是悄悄退回默认：
#: 请求了某个排序却没得到它的调用方无从察觉，而在自己没要的排序下翻页，还可能整行整行地
#: 漏掉数据。422 在路由层抛出 —— 那里把 `sort` 声明为恰好覆盖这些取值的 `Literal`；下面的
#: 默认值是 *直接* 调用 `list_repositories` 的调用方所得到的，刻意不作为强制点。
SORT_OPTIONS: Final[tuple[str, ...]] = (SORT_STARS, SORT_FORKS, SORT_LAST_SEEN)


@dataclass(frozen=True, slots=True)
class RepositoryCatalogEntry:
    """一个仓库，连同它的 provider，以及（在按主题筛选时）它的排名。"""

    repository: Repository
    provider: str
    score: Decimal | None
    rank: int | None
    selected_for_agent: bool | None


@dataclass(frozen=True, slots=True)
class RepositoryPage:
    """目录中的一页，外加匹配筛选条件的总数。"""

    items: tuple[RepositoryCatalogEntry, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """把请求的页大小限制在有文档的 1-100 范围内。"""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def _catalog_conditions(
    *,
    topic_id: int | None,
    language: str | None,
    min_stars: int | None,
    first_seen_after: datetime | None,
    query: str | None,
) -> list[ColumnElement[bool]]:
    """构造分页查询与其计数查询共用的筛选条件列表。

    共用这份列表正是重点：计数若用了与分页不同的谓词，报出的 `total` 就会是调用方怎么
    翻都翻不完的数。
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
        pattern = f"%{literal_like(query.strip().lower())}%"
        conditions.append(
            or_(
                func.lower(Repository.full_name).like(pattern, escape=LIKE_ESCAPE_CHAR),
                func.lower(func.coalesce(Repository.description, "")).like(
                    pattern, escape=LIKE_ESCAPE_CHAR
                ),
            )
        )
    return conditions


def _order_by(sort: str) -> tuple[Any, ...]:
    """返回某个有文档的 `sort` 取值对应的排序。

    `id` 永远是次级排序键：star 数相同的两个仓库很常见（刚采集的一批往往从零开始），
    没有全序时，数据库在跨页时可能以不同顺序返回它们，于是重复一个、漏掉另一个。
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
    """返回仓库目录的一页。

    目录只放公开元数据（`docs/security-and-privacy.md` §2），因此这里没有逐行的访问谓词。
    `topic_id` 则不同：它把结果收窄到某个主题的候选列表，而该主题对调用方是否可见，是在
    调用本函数 *之前* 由 `security/authorization.py:authorize_topic_read` 决定的。
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
    """返回一条仓库记录；不存在时返回 `None`。"""
    return session.get(Repository, repository_id)


def list_snapshots(
    session: Session,
    *,
    repository_id: int,
    start_date: date,
    end_date: date,
) -> tuple[RepoSnapshot, ...]:
    """返回某个仓库在一个闭区间日期窗口内的快照序列。

    窗口两端都包含，与 `pipeline/snapshots.py:get_snapshot_series` 一致 —— 这里是通过调用
    它而不是重新实现：`snapshot_date` 是自然日，半开区间会悄悄丢掉调用方指定为结束的那
    一天。
    """
    return tuple(
        get_snapshot_series(
            session,
            repository_id,
            start_date=start_date,
            end_date=end_date,
        )
    )
