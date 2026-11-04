"""主题端点：监控范围的配置。

`docs/api-contract.md` §3.2 是契约；本模块只整形请求与响应，把每个决定都委托出去。有两处委托
选择值得点名：

* **单行读取走 `authorize_topic_read`**，与仓库目录校验 `topic_id` 筛选时用的是同一个函数。
  「这个调用方能不能看这个主题」只有一份实现，而且不在本文件里。
* **写操作调用服务，让服务抛出的异常自己携带答案。** 主题不存在经由 `TopicError` 处理器变成
  `404`，而不是靠路由里的一次预取，这样一个操作只做一次存在性检查，而不是两次可能互相打脸的
  检查。

这里没有任何东西会触发研究运行。`POST /topics/{id}/run` 是故意不存在的：
一次运行要花模型预算，这个触发权属于调度器与运维 CLI（§3.2）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.security.authorization import (
    TOPIC_READ_ROLES,
    TOPIC_WRITE_ROLES,
    authorize_topic_read,
)
from open_source_intelligence.security.dependencies import (
    get_db_session,
    request_id,
    require_roles,
)
from open_source_intelligence.topics import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    create_topic,
    delete_topic,
    list_topics,
    require_usable_name,
    update_topic,
)

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])

ReadUser = Annotated[User, Depends(require_roles(*TOPIC_READ_ROLES))]
WriteUser = Annotated[User, Depends(require_roles(*TOPIC_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

PublishMode = Literal["AUTO", "AUTO_WITH_DELAY", "MANUAL"]

TopicTerm = Annotated[str, Field(min_length=1, max_length=64)]


def _require_usable_name(value: str) -> str:
    """用写入方自己对「可用名字」的定义校验 `name` 字段。

    函数体只有一句对 `topics.require_usable_name` 的调用，这正是重点：边界
    不复述规则，而是调用规则。`require_usable_name` 抛的是 `TopicNameBlank`
    （一个 `ValueError`），于是 Pydantic 把这次拒绝变成点名字段的 `422`，
    而直接调用服务的脚本收到的也是同一个异常。
    """
    return require_usable_name(value)


#: 在边界上完成校验与去空白的主题名。
TopicName = Annotated[
    str,
    AfterValidator(_require_usable_name),
    Field(min_length=1, max_length=128),
]


class TopicResponse(BaseModel):
    """一个监控主题。"""

    id: int
    name: str
    description: str | None
    languages: list[str] | None
    keywords: list[str] | None
    min_stars: int
    window_days: int
    max_candidates: int
    publish_mode: str
    enabled: bool
    created_by: int
    created_at: datetime
    updated_at: datetime


class TopicListResponse(BaseModel):
    """一页可见的主题。"""

    items: list[TopicResponse]
    page: int
    page_size: int
    total: int


class _TopicFields(BaseModel):
    """主题请求里可选的那一半字段：两个必填列之外的所有内容。

    做成共享基类，而不是让更新模型继承创建模型。继承会逼 `PATCH` 把 `name` 与 `publish_mode`
    重新声明为可选，而放宽继承来的字段正是类型检查器唯一拒绝的覆写方向 ——
    它拒绝得对，因为持有子类的调用方从此就能清空基类保证有值的字段。只抽出
    两个请求真正共享的字段，就能让 `POST` 保持必填、`PATCH` 保持可选，而
    两个模型互不矛盾。
    """

    description: str | None = Field(default=None, max_length=2000)
    languages: list[TopicTerm] | None = Field(default=None, max_length=32)
    keywords: list[TopicTerm] | None = Field(default=None, max_length=32)
    min_stars: int | None = Field(default=None, ge=0)
    window_days: int | None = Field(default=None, ge=1, le=365)
    max_candidates: int | None = Field(default=None, ge=1, le=200)
    enabled: bool | None = None


class TopicCreateRequest(_TopicFields):
    """新建主题必须携带的字段。

    每个可选字段默认都是 `None`，请求模型也不复述任何一列的默认值：调用方没发过来的字段不会
    出现在 `model_dump(exclude_unset=True)` 里，于是「新主题长什么样」始终只由列定义一次。
    """

    name: TopicName
    publish_mode: PublishMode


class TopicUpdateRequest(_TopicFields):
    """`PATCH` 可以携带的字段：同一组字段，全部可选。

    ``name`` 与 ``publish_mode`` 在这里变成可选，这也是「新建主题」与「修改主题」之间唯一的
    差别。
    """

    name: TopicName | None = None
    publish_mode: PublishMode | None = None


class TopicDeleteResponse(BaseModel):
    """删除一个未被引用的主题后的结果。"""

    topic_id: int
    deleted: bool
    unlinked_candidate_count: int


def _to_response(topic: Topic) -> TopicResponse:
    """把一个主题行整形为对外响应。

    `languages_json` / `keywords_json` 以 `languages` / `keywords` 的名字
    出去。`_json` 后缀命名的是存储形态而不是字段本身 —— 目录层对
    `topics_json` 做了同样的取舍。列表为 null 表示「没有这一类限制」，列未
    设置时存的就是这个值。
    """
    return TopicResponse(
        id=topic.id,
        name=topic.name,
        description=topic.description,
        languages=list(topic.languages_json) if topic.languages_json else None,
        keywords=list(topic.keywords_json) if topic.keywords_json else None,
        min_stars=topic.min_stars,
        window_days=topic.window_days,
        max_candidates=topic.max_candidates,
        publish_mode=topic.publish_mode,
        enabled=topic.enabled,
        created_by=topic.created_by,
        created_at=topic.created_at,
        updated_at=topic.updated_at,
    )


@router.get("", response_model=TopicListResponse)
def get_topics(
    user: ReadUser,
    session: DbSession,
    enabled: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> TopicListResponse:
    """返回一页该调用方可见的主题。

    非 `ADMIN` 传 `enabled=false` 得到的是空页而不是 `403`：可见性规则与请求的筛选取交集，而
    「你能看到的主题里没有停用的」正是对所问问题的真实回答。
    """
    result = list_topics(
        session,
        user=user,
        enabled=enabled,
        query=q,
        page=page,
        page_size=page_size,
    )
    return TopicListResponse(
        items=[_to_response(topic) for topic in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get("/{topic_id}", response_model=TopicResponse)
def get_topic(
    request: Request,
    topic_id: int,
    user: ReadUser,
    session: DbSession,
) -> TopicResponse:
    """返回一个主题；拒绝方式与目录的 `topic_id` 筛选完全相同。"""
    topic = authorize_topic_read(session, request, user, topic_id=topic_id, action="topic.read")
    return _to_response(topic)


@router.post("", response_model=TopicResponse, status_code=status.HTTP_201_CREATED)
def create_topic_endpoint(
    payload: TopicCreateRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicResponse:
    """新建一个监控主题。"""
    provided = payload.model_dump(exclude_unset=True)
    name = provided.pop("name")
    publish_mode = provided.pop("publish_mode")
    topic = create_topic(
        session,
        actor_id=user.id,
        name=name,
        publish_mode=publish_mode,
        values=provided,
        request_id=request_id(request),
    )
    return _to_response(topic)


@router.patch("/{topic_id}", response_model=TopicResponse)
def update_topic_endpoint(
    topic_id: int,
    payload: TopicUpdateRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicResponse:
    """把调用方发来的字段应用到某个主题上。

    `exclude_unset=True` 是有承重作用的：正是它让「没发 `description`」和「发了
    `description: null`」成为两个不同的请求，而不是一个。
    """
    topic, _changed = update_topic(
        session,
        actor_id=user.id,
        topic_id=topic_id,
        values=payload.model_dump(exclude_unset=True),
        request_id=request_id(request),
    )
    return _to_response(topic)


@router.delete("/{topic_id}", response_model=TopicDeleteResponse)
def delete_topic_endpoint(
    topic_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> TopicDeleteResponse:
    """删除一个未被引用的主题；若有表仍指向它就拒绝，并说明是哪张表。"""
    unlinked = delete_topic(
        session,
        actor_id=user.id,
        topic_id=topic_id,
        request_id=request_id(request),
    )
    return TopicDeleteResponse(
        topic_id=topic_id,
        deleted=True,
        unlinked_candidate_count=unlinked,
    )
