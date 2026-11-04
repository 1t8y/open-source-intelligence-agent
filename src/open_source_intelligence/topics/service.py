"""主题写入：创建、更新与删除，每个都带自己的审计行。

写入放在这里而不是放进路由，是为了让这三个操作也能从脚本或运维界面到
达，而不必经过 HTTP。路由负责把请求塑形，并把调用方实际发来的字段映射
交过来；判断这些字段意味着什么，是本模块的职责，而这个含义不随传输方式
改变。

有四条规则值得说明，因为每一条都是「显然的实现方式是错的」的地方：

* **名字在去空白之后比较，而空名字在这里就被拒绝。** 唯一性由数据库执
  行，否则一个前后带空格的名字与同名但不带空格的名字，会变成两行说着同
  一件事的记录，而唯一检查唯一能看到的地方只看到一个。`normalize_name`
  是存储形式的唯一定义，`require_usable_name` 是可接受形式的唯一定义，
  请求边界调用的也是同样这两个函数 —— 于是检查与写入不可能不一致。拒绝
  放在本模块而不只放在边界，是因为下面的函数也是脚本写主题的途径，一个
  从不经过 Pydantic 模型的调用方，否则就会存下一行没有名字的记录。
* **被历史引用的删除会被拒绝，而不是连级删除。** `agent_runs`、
  `pipeline_runs`、`reports` 与 `report_drafts` 以
  `ON DELETE RESTRICT` 指向 `topics.id`，它们记录的是这个主题产出过
  什么。停用一个有过去的主题，才是调用方真正想要的操作；删除它要么在数
  据库上失败，要么（如果放宽了约束）删掉「发生过什么」的记录。
* **被拒绝的写入与被接受的写入一样要审计。** 下面每一次拒绝都会在抛异常
  之前写一行 `failed`。「谁试图占用一个已被使用的名字」与「谁试图删除一
  个产出过报告的主题」，是只有拒绝记录才能回答的问题；只留成功的轨迹回
  答不了。
* **被拒绝的创建没有资源可指。** 那时还没有主题，所以 `resource_id` 为
  空，尝试的名字改放在 `metadata_json` 里。这与授权层记录的是同一种不
  对称：请求范围内的拒绝本来就没有东西可指，为它编一个 id 只会让轨迹说
  谎。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.agent import AgentRun
from open_source_intelligence.persistence.models.intelligence import (
    PipelineRun,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import Report, ReportDraft
from open_source_intelligence.security.audit import record_audit_event

from .errors import TopicError, TopicInUse, TopicNameBlank, TopicNameConflict, TopicNotFound

#: API 字段名 -> ORM 属性。两个列表字段带 `_json` 后缀存储，这是存储细
#: 节而不是字段含义的一部分：契约对外把它们输出为 `languages` /
#: `keywords`，与目录里的 `topics_json` 在出口变成 `topics` 是同一个约
#: 定。把映射集中在一张表里，意味着路由永远不必知道列名，本模块也永远不
#: 必知道 API 名。
FIELD_COLUMNS: Final[dict[str, str]] = {
    "name": "name",
    "description": "description",
    "languages": "languages_json",
    "keywords": "keywords_json",
    "min_stars": "min_stars",
    "window_days": "window_days",
    "max_candidates": "max_candidates",
    "publish_mode": "publish_mode",
    "enabled": "enabled",
}

#: 引用 `topics.id` 的表，按拒绝消息点名它们的顺序排列。固定下来而不是
#: 动态发现，是为了让同一次拒绝在每个部署上都读起来一样 —— 这才让它值得
#: 写进契约的示例里。
REFERENCE_TABLES: Final[tuple[tuple[str, type[Any]], ...]] = (
    ("agent_runs", AgentRun),
    ("pipeline_runs", PipelineRun),
    ("reports", Report),
    ("report_drafts", ReportDraft),
)


def normalize_name(name: str) -> str:
    """返回主题名的存储形式：去掉首尾空白。

    一个纯粹的归一化函数：它回答「会被存成什么」，而不是「能不能存」。
    拒绝留在 `require_usable_name` 里，这样只想比较名字的调用方，不必为
    了比较而被迫处理异常。
    """
    return name.strip()


def require_usable_name(name: str) -> str:
    """返回名字的存储形式，并在去空白后为空时拒绝它。

    刻意从两处调用 —— 请求边界用这个函数校验 `name` 字段，下面的写入在
    存之前也调它。把两个出口拆开，正是一个没有名字的主题溜进来的方式：
    端点并不是唯一的写入方（模块 docstring 已经说明脚本与运维界面走的是
    同一批函数），而过去只有其中一条路径做了检查。
    """
    normalized = normalize_name(name)
    if not normalized:
        raise TopicNameBlank("name must not be blank")
    return normalized


def _normalize_value(field: str, value: Any) -> Any:
    """返回一个传入字段值的存储形式。"""
    if field == "name":
        return require_usable_name(value)
    if field in ("languages", "keywords"):
        # 空列表与 `null` 对主题过滤条件来说意思相同（「不限语言 / 关键字」），
        # 所以存法也相同。在这里把它们合并，才能让一次清空过滤条件的 PATCH，
        # 不会在此后每次请求里都看起来像一次变更。
        return [str(item).strip() for item in value] if value else None
    return value


def _stored_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """把传入的 API 字段翻译成构造调用所需的 ORM 属性值。"""
    translated: dict[str, Any] = {}
    for field, value in values.items():
        translated[_column_for(field)] = _normalize_value(field, value)
    return translated


def _column_for(field: str) -> str:
    """返回一个 API 字段对应的 ORM 属性，并拒绝契约未定义的名字。"""
    column = FIELD_COLUMNS.get(field)
    if column is None:
        raise ValueError(f"unknown topic field {field!r}")
    return column


def _refuse(
    session: Session,
    *,
    action: str,
    actor_id: int,
    error: TopicError,
    request_id: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> NoReturn:
    """记录一次被拒绝的写入、提交轨迹，然后重新抛出。

    这里的提交不是可以省掉的优化：本函数的调用方马上就要抛异常，而
    session 会在退出的路上被关闭。此刻仍停留在 session 待处理状态的审计
    行会被回滚，于是每一次拒绝都变得不可见 —— 这恰恰是这个辅助函数存在
    要防的失败。

    `request_id` 是透传进来的，而不是在这里查的，因为本模块没有请求：
    HTTP 路由有，脚本没有，而脚本给出的 `None` 就是诚实的答案。它重
    要，是因为轨迹的职责是回答「谁尝试了这件事」，没有这个关联 id，那
    一行就无法与记录了对方发了什么的请求日志连起来。
    """
    record_audit_event(
        session,
        action=action,
        resource_type="topic",
        resource_id=resource_id,
        result="failed",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json=metadata,
    )
    session.commit()
    raise error


def _ensure_name_is_free(session: Session, name: str, *, keep_topic_id: int | None = None) -> None:
    """拒绝一个已被别的主题占用的名字，可选择忽略某一行。"""
    statement = select(Topic.id).where(Topic.name == name)
    if keep_topic_id is not None:
        statement = statement.where(Topic.id != keep_topic_id)
    if session.scalar(statement) is not None:
        raise TopicNameConflict(f"topic name {name!r} is already taken")


def reference_counts(session: Session, topic_id: int) -> dict[str, int]:
    """统计每张引用表里指向 `topic_id` 的行数。

    之所以公开，是因为两个调用方需要同一个答案：删除守卫读它来做判断，
    而任何要解释「这个主题为什么删不掉」的地方，都想展示同样的数字，而
    不是自己再推一遍。
    """
    counts: dict[str, int] = {}
    for label, model in REFERENCE_TABLES:
        counts[label] = (
            session.scalar(
                select(func.count()).select_from(model).where(model.topic_id == topic_id)
            )
            or 0
        )
    return counts


def create_topic(
    session: Session,
    *,
    actor_id: int,
    name: str,
    publish_mode: str,
    values: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> Topic:
    """创建一个主题并记录这次变更。

    `name` 与 `publish_mode` 单独作为关键字参数，因为它们是表要求的两
    列；其余字段只在调用方确实发来时通过 `values` 到达，这样列的默认值
    仍然是「新主题长什么样」的唯一定义。在这里重述那些默认值，就是一个
    默认值最终有两种含义的由来。

    `created_by` 是唯一没有默认值的必填列，所以把操作者写进去，而不是留
    给数据库：审计行与它所描述的那一行于是指向同一个账号，「这个主题是
    谁加的」才能从两边任一得到回答。
    """
    try:
        normalized = require_usable_name(name)
    except TopicNameBlank as error:
        _refuse(
            session,
            action="topic.create",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            metadata={"reason": error.code},
        )
    try:
        _ensure_name_is_free(session, normalized)
    except TopicNameConflict as error:
        _refuse(
            session,
            action="topic.create",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            metadata={"name": normalized, "reason": error.code},
        )
    topic = Topic(
        name=normalized,
        publish_mode=publish_mode,
        created_by=actor_id,
        **(_stored_fields(values) if values else {}),
    )
    session.add(topic)
    try:
        session.flush()
    except IntegrityError:
        # 上面的唯一性检查不是锁，两个调用方可以一起通过它。在加入审计行
        # 之前必须回滚：已经失败的那个事务带不了它。
        #
        # 重新检查才是这个处理器诚实的地方。「`IntegrityError` 就意味着名字
        # 被占了」是最初的版本，而它错在会掩盖 bug 的方向上：缺少必填列同样
        # 会抛 `IntegrityError`，于是一次坏掉的插入被报成
        # `409 TOPIC_NAME_CONFLICT`，调用方跑去换了个名字。只有此刻确实已被
        # 占用的名字才算冲突；其它情况一律重新抛出，成为它本来的 `500`。
        session.rollback()
        try:
            _ensure_name_is_free(session, normalized)
        except TopicNameConflict:
            _refuse(
                session,
                action="topic.create",
                actor_id=actor_id,
                error=TopicNameConflict(f"topic name {normalized!r} is already taken"),
                request_id=request_id,
                metadata={"name": normalized, "reason": "TOPIC_NAME_CONFLICT", "race": True},
            )
        raise
    record_audit_event(
        session,
        action="topic.create",
        resource_type="topic",
        resource_id=str(topic.id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={
            "name": topic.name,
            "publish_mode": topic.publish_mode,
            "enabled": topic.enabled,
        },
    )
    session.commit()
    return topic


def update_topic(
    session: Session,
    *,
    actor_id: int,
    topic_id: int,
    values: Mapping[str, Any],
    request_id: str | None = None,
) -> tuple[Topic, tuple[str, ...]]:
    """应用调用方发来的字段，返回主题与被改动字段的名字。

    只动 `values` 里出现的字段，所以 `values` 就是请求边界从
    `model_dump(exclude_unset=True)` 得到的东西：「没发」与「发了 null」
    是两个不同的请求（`null` 会清空描述），把二者合并，会让其中一种变
    得不可能。

    一个所有字段本来就已经等于请求值的请求，是成功，`changed` 为空元
    组，而不是冲突。把一次无变化的修改报成 `409`，会让一个幂等重复的
    PATCH，变成客户端必须读一条错误才能理解的东西。

    `changed` 点名的是 **API** 字段而非列名：它是对请求的描述，一个发了
    `languages` 的调用方，不该被告知它改动了 `languages_json`。
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise TopicNotFound(f"topic {topic_id} does not exist")
    changed: list[str] = []
    try:
        for field, raw in values.items():
            column = _column_for(field)
            value = _normalize_value(field, raw)
            if getattr(topic, column) != value:
                setattr(topic, column, value)
                changed.append(field)
    except TopicNameBlank as error:
        # 同一个 PATCH 里靠前的字段可能已经暂存在实例上了；回滚会丢弃它们，
        # 于是一次被拒绝的请求什么都不改，而不是改到被拒那个字段之前为止。
        session.rollback()
        _refuse(
            session,
            action="topic.update",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            resource_id=str(topic_id),
            metadata={"reason": error.code},
        )
    if "name" in changed:
        # 先取成普通字符串：下面的回滚会让 `topic` 过期，之后再读
        # `topic.name` 得到的会是数据库里它还留着的名字，而不是调用方被拒绝
        # 的那个名字。
        attempted = topic.name
        try:
            _ensure_name_is_free(session, attempted, keep_topic_id=topic.id)
        except TopicNameConflict as error:
            # 改名已经暂存但还没 flush。不先回滚就提交审计行，会把那条
            # 冲突的 UPDATE 一起带上，并在唯一约束上失败，于是一个具名
            # 冲突变成了 500。
            session.rollback()
            _refuse(
                session,
                action="topic.update",
                actor_id=actor_id,
                error=error,
                request_id=request_id,
                resource_id=str(topic_id),
                metadata={"name": attempted, "reason": error.code},
            )
    record_audit_event(
        session,
        action="topic.update",
        resource_type="topic",
        resource_id=str(topic.id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={"changed_fields": changed},
    )
    session.commit()
    return topic, tuple(changed)


def delete_topic(
    session: Session,
    *,
    actor_id: int,
    topic_id: int,
    request_id: str | None = None,
) -> int:
    """删除一个未被引用的主题，并返回随之移除的候选关联数。

    只要有记录表引用该主题就拒绝，并为这次拒绝留下审计。关联的
    `topic_repositories` 行是某一轮排名的产物而不是历史，所以随主题一
    起删掉 —— 返回这个计数，是因为删主题与删它的排名是两件事，调用方两
    件都该知道。
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise TopicNotFound(f"topic {topic_id} does not exist")
    counts = reference_counts(session, topic.id)
    blocking = [label for label, count in counts.items() if count]
    # 赶在这一行消失之前读出标识值：提交之后就取不到了。
    retained_id = topic.id
    retained_name = topic.name
    if blocking:
        _refuse(
            session,
            action="topic.delete",
            actor_id=actor_id,
            error=TopicInUse(f"topic {retained_id} is referenced by {', '.join(blocking)}"),
            request_id=request_id,
            resource_id=str(retained_id),
            metadata={"name": retained_name, "references": counts},
        )
    unlinked = (
        session.scalar(
            select(func.count())
            .select_from(TopicRepository)
            .where(TopicRepository.topic_id == retained_id)
        )
        or 0
    )
    session.delete(topic)
    record_audit_event(
        session,
        action="topic.delete",
        resource_type="topic",
        resource_id=str(retained_id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={"name": retained_name, "unlinked_candidate_count": unlinked},
    )
    session.commit()
    return unlinked
