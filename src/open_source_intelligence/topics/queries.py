"""主题端点背后的读侧查询。

`pipeline/queries.py`、`reports/queries.py` 与
`observability/queries.py` 立下了本模块沿用的先例：API 与 Web 层调用这
些函数，从不自己发 SQL（`INTERFACES.md` §4）。这里不写任何一行，也不
需要任何迁移 —— 它读的每一列本来就已经存在。

两个决定是承重的：

* **可见性规则不被重新实现。** `ADMIN` 能看到所有主题，其他人只看到
  启用的那些：这就是 `security/authorization.py:can_read_topic`，被改
  写成 SQL 谓词，好让过滤条件进到数据库，而不是在取回之后再跑一遍。
  一个在 Python 里丢掉不可见行的列表，仍然会在 `total` 里把它们数进
  去，等于回答了调用方无权提出的问题。
* **`q` 是字面子串搜索**，用仓库目录所用的同一个辅助函数转义
  （`core/text.py`）。两个调用方，一份对「转义」含义的定义。
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

#: 文档化的列表契约把 `page_size` 限制在 1-100；同样的边界也施加在这里，
#: 好让平台上每个列表端点都以同样的方式拒绝一个没有边界的页。
DEFAULT_PAGE_SIZE: Final[int] = 20
MAX_PAGE_SIZE: Final[int] = 100


@dataclass(frozen=True, slots=True)
class TopicPage:
    """一页主题，外加符合过滤条件的总数。"""

    items: tuple[Topic, ...]
    page: int
    page_size: int
    total: int


def clamp_page_size(page_size: int) -> int:
    """把请求的页大小限制在文档写明的 1-100 区间内。"""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def _topic_conditions(
    *,
    user: User,
    roles: set[str],
    enabled: bool | None,
    query: str | None,
) -> list[ColumnElement[bool]]:
    """构建分页查询与其计数查询共用的过滤条件列表。

    共用这一列表才是要点：一个用了与分页不同谓词的计数，会报出一个调用
    方永远翻不到底的 `total`。在这里，共用的谓词也正是把不可见主题挡在
    `total` 之外的东西。
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
    """返回一页可见主题，按每日流水线读取它们的顺序排列。

    这里的 `id` 升序并非随意：`pipeline/daily.py:list_enabled_topics`
    用的就是它，于是「列表的顺序」与「流水线运行的顺序」是同一个顺序，
    而不是两个各自都合理的顺序。
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
