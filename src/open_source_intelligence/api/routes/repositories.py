"""仓库目录端点：共享的仓库列表与单个仓库的走势。

`docs/api-contract.md` §4 固定了这两条路由要回答什么。有两条性质值得写进代码，因为它们之后很
容易被改坏：

* **目录是公开的。** `docs/security-and-privacy.md` §2 把公开仓库元数据归为
  公开数据，因此这里没有逐行的访问谓词，每个已认证角色读到的都是同一份
  列表。唯一需要裁定的是这份列表可以按哪个主题切片，而那个决定来自
  `security.authorization.authorize_topic_read`。
* **窗口是闭区间的日期范围。** `pipeline/queries.list_snapshots` 委托给
  `get_snapshot_series`，因此这条路由对范围从哪儿开始、到哪儿结束不持自己的意见。
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

#: 调用方不指定窗口时，走势路由往回看多远。三十天足够覆盖周报的七日窗口好几
#: 遍，又不至于让一个默认值变成对一张「每仓每天长一行」的表的无界扫描。
DEFAULT_SNAPSHOT_WINDOW_DAYS = 30

#: 走势路由愿意提供的最长窗口。这里做的是校验而不是截断：截断后的答案会悄悄
#: 变成另一段时间，而两个数字看起来都合理，于是错误没人察觉。
MAX_SNAPSHOT_WINDOW_DAYS = 366


class RepositorySummaryResponse(BaseModel):
    """目录里的一行：仓库及其来源，按主题切片时还带排名。"""

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
    """一页仓库目录。"""

    items: list[RepositorySummaryResponse]
    page: int
    page_size: int
    total: int


class SnapshotResponse(BaseModel):
    """仓库走势中的一天。"""

    snapshot_date: str
    stars: int
    forks: int
    open_issues: int | None
    stars_delta: int | None
    forks_delta: int | None


class SnapshotSeriesResponse(BaseModel):
    """一个仓库在解析出的闭区间窗口内的快照序列。"""

    repository_id: int
    provider: str
    full_name: str
    start_date: str
    end_date: str
    granularity: str
    items: list[SnapshotResponse]


def _summary(entry: RepositoryCatalogEntry) -> RepositorySummaryResponse:
    """序列化一条目录条目。

    `score` 以文本形式输出，理由和 Agent 台账里的 `estimated_cost` 一样：它是
    `NUMERIC(12,6)`，渲染成 JSON 浮点数会让读回来的值与存进去的值不一致。
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
    """返回一页仓库目录。

    `topic_id` 是唯一一个携带授权决定的筛选条件：它把列表收窄到某个主题
    的候选集，而调用方无权查看的主题会在查询执行之前就被拒绝。
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
    """返回一个仓库在闭区间日期窗口内的快照序列。

    空窗口是 `200` 加一个空列表，而不是 `404`：仓库确实存在，诚实的答案是
    「它在这个区间里没有快照」。仓库不存在才是 `404`。
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
