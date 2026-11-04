"""主题写入的服务层用例。

`topics/service.py` 是 `topics` 唯一的写入方，`test_topic_routes.py`
则通过 API 用它。
这里的用例钉住 API 触不到、也分辨不出的那几处：

* **不经过 Pydantic 模型的写入，也必须以同样的方式被拒。** 模块自己的 docstring
  说这些操作可以从脚本和运维界面调用，而这两条路都不经过请求边界 ——
  所以去空白规则与空名规则在这里也得成立，不能只在那边成立。
* **`IntegrityError` 不自动等于名字冲突。** 这个模块的第一版把所有
  `IntegrityError` 都捕获下来抛 `TopicNameConflict`，
  等于把一次坏掉的插入变成「换一个名字吧」的建议。
  下面的回归用例正是揪出它的探针。
* **「不能删」是数出来的，不是靠捕获数据库的拒绝。** 因此哪些表算数
  是本模块要做的决定，也就必须有测试把它写清楚。

三种失败靠 `code` 区分，因为 `api/errors.py` 正是拿它映射状态码，
调用方也正是按它分支。
"""

from __future__ import annotations

import pytest
from api_support import Actor, create_actor
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import Topic, TopicRepository
from open_source_intelligence.topics import (
    TopicInUse,
    TopicNameBlank,
    TopicNameConflict,
    TopicNotFound,
    create_topic,
    delete_topic,
    reference_counts,
    update_topic,
)

#: 账号夹具的用户名放在这里，免得本包的两个测试模块
#: 在灌同一种内存数据库形态时撞车。
ADMIN_USERNAME = "topic-service-admin"


@pytest.fixture()
def admin(session: Session) -> Actor:
    """这里每一次写入都记在它名下的那个账号。"""
    return create_actor(session, username=ADMIN_USERNAME, role="ADMIN")


def _create(session: Session, admin: Actor, name: str, **values: object) -> Topic:
    """经由服务创建一个主题，把断言要用到的调用压短。"""
    return create_topic(
        session,
        actor_id=admin.user_id,
        name=name,
        publish_mode="MANUAL",
        values=values or None,
    )


def _rows(session: Session, action: str, result: str | None = None) -> list[AuditLog]:
    """按插入顺序返回某个动作的审计行，可选只取某种结果。"""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    if result is not None:
        statement = statement.where(AuditLog.result == result)
    return list(session.scalars(statement).all())


# --------------------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------------------


def test_create_writes_the_row_and_its_audit(session: Session, admin: Actor) -> None:
    """证明创建成功会留下归在操作者名下的一行，以及一条 `success` 审计行。"""
    topic = _create(session, admin, "vector-db")

    assert topic.id is not None
    assert topic.created_by == admin.user_id
    assert topic.enabled is True
    rows = _rows(session, "topic.create")
    assert len(rows) == 1
    assert rows[0].result == "success"
    assert rows[0].resource_type == "topic"
    assert rows[0].resource_id == str(topic.id)
    assert rows[0].actor_id == admin.user_id
    assert rows[0].metadata_json == {
        "name": "vector-db",
        "publish_mode": "MANUAL",
        "enabled": True,
    }


def test_create_attributes_the_row_to_the_actor(session: Session, admin: Actor) -> None:
    """证明 `created_by` 由服务写入，因为它是必填且无默认值的列。

    留给数据库的话每次插入都会失败，而这个失败是 `IntegrityError`，
    旧的处理逻辑会把它报成「名字已被占用」。这条断言与上面那条审计断言分开写，
    是为了让回归指向出错的那个字段，而不是指向行数。
    """
    topic = _create(session, admin, "attributed")
    assert topic.created_by == admin.user_id


def test_a_direct_call_records_no_request_id(session: Session, admin: Actor) -> None:
    """证明脚本没有请求，于是该列为空 —— 这才是诚实的取值。

    这是路由侧用例的另一半：`request_id` 由手里有它的调用方传进来，
    而不是查出来，这才让本模块在 HTTP 之外也能用，
    而不必编造一个轨迹里没人会共享的关联 id。
    """
    _create(session, admin, "no-request")
    assert _rows(session, "topic.create", "success")[0].request_id is None
    with pytest.raises(TopicNameConflict):
        _create(session, admin, "no-request")
    assert _rows(session, "topic.create", "failed")[0].request_id is None


def test_a_supplied_request_id_is_stored(session: Session, admin: Actor) -> None:
    """证明调用方手里真有关联 id 时，它会落到行上。"""
    topic = create_topic(
        session,
        actor_id=admin.user_id,
        name="correlated",
        publish_mode="MANUAL",
        request_id="drill-0003",
    )
    update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"enabled": False},
        request_id="drill-0003",
    )
    delete_topic(session, actor_id=admin.user_id, topic_id=topic.id, request_id="drill-0003")

    rows = [
        row
        for row in session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        if row.action.startswith("topic.")
    ]
    assert [row.action for row in rows] == ["topic.create", "topic.update", "topic.delete"]
    assert {row.request_id for row in rows} == {"drill-0003"}


def test_create_trims_the_name_before_it_stores_it(session: Session, admin: Actor) -> None:
    """证明落库的是去过空白的形式，唯一性检查才因此有意义。"""
    assert _create(session, admin, "  向量数据库  ").name == "向量数据库"


def test_create_only_writes_the_values_the_caller_sent(session: Session, admin: Actor) -> None:
    """证明列默认值仍是「新主题长什么样」的唯一出处。"""
    topic = _create(session, admin, "partial", min_stars=250, languages=["Rust"])

    assert topic.min_stars == 250
    assert topic.languages_json == ["Rust"]
    assert topic.window_days == 7
    assert topic.max_candidates == 30
    assert topic.description is None
    assert topic.keywords_json is None


def test_create_refuses_a_blank_name_and_names_nothing(session: Session, admin: Actor) -> None:
    """证明只有空格的名字会被拒，而且没有主题可供审计行指向。"""
    with pytest.raises(TopicNameBlank) as raised:
        _create(session, admin, "   ")

    assert raised.value.code == "TOPIC_NAME_BLANK"
    assert session.scalars(select(Topic)).all() == []
    rows = _rows(session, "topic.create")
    assert len(rows) == 1
    assert rows[0].result == "failed"
    assert rows[0].resource_id is None
    assert rows[0].metadata_json == {"reason": "TOPIC_NAME_BLANK"}


def test_create_refuses_a_taken_name_and_audits_the_refusal(session: Session, admin: Actor) -> None:
    """证明拒绝会留进轨迹：只有它才能回答「还有谁想要这个名字」。"""
    _create(session, admin, "taken")
    with pytest.raises(TopicNameConflict) as raised:
        _create(session, admin, "taken")

    assert raised.value.code == "TOPIC_NAME_CONFLICT"
    assert len(session.scalars(select(Topic).where(Topic.name == "taken")).all()) == 1
    failures = _rows(session, "topic.create", "failed")
    assert len(failures) == 1
    assert failures[0].metadata_json == {"name": "taken", "reason": "TOPIC_NAME_CONFLICT"}
    assert len(_rows(session, "topic.create", "success")) == 1


def test_create_does_not_relabel_an_unrelated_integrity_error(
    session: Session, admin: Actor
) -> None:
    """证明不是唯一名约束的违规会原样抛出，而不是被改成别的名字。

    `publish_mode` 带 CHECK 约束，所以枚举之外的值是一次坏写入，
    而不是名字被占了。
    端点造不出这种情况 —— 请求模型约束了这个字段 —— 这恰恰是它在这里要紧的原因：
    直接调用方没有模型替他翻译答案，而「409 TOPIC_NAME_CONFLICT」
    会让它去为一个跟名字毫无关系的问题换个名字。
    """
    with pytest.raises(IntegrityError):
        create_topic(
            session,
            actor_id=admin.user_id,
            name="broken-mode",
            publish_mode="SOMETIMES",
        )

    assert session.scalars(select(Topic)).all() == []
    assert _rows(session, "topic.create") == []


# --------------------------------------------------------------------------------------
# 更新
# --------------------------------------------------------------------------------------


def test_update_applies_only_the_fields_it_was_given(session: Session, admin: Actor) -> None:
    """证明 `values` 就是调用方发来的内容，没提到的字段保持原值。"""
    topic = _create(session, admin, "upd", description="before", min_stars=10)

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"min_stars": 20},
    )

    assert changed == ("min_stars",)
    assert updated.min_stars == 20
    assert updated.description == "before"
    assert updated.name == "upd"


def test_update_names_the_api_field_and_not_the_column(session: Session, admin: Actor) -> None:
    """证明 `changed_fields` 描述的是请求，而 `languages_json` 只是存储细节。

    主题列表的出口出于同样的理由去掉了 `_json` 后缀，
    这样读审计行的调用方看到的是自己发过去的字段名，而不是自己从没提过的名字。
    """
    topic = _create(session, admin, "upd-names")

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"languages": ["Go"]},
    )

    assert changed == ("languages",)
    assert updated.languages_json == ["Go"]
    rows = _rows(session, "topic.update")
    assert len(rows) == 1
    assert rows[0].result == "success"
    assert rows[0].resource_id == str(topic.id)
    assert rows[0].metadata_json == {"changed_fields": ["languages"]}


def test_update_of_an_unchanged_value_is_a_success_with_no_change(
    session: Session,
    admin: Actor,
) -> None:
    """证明重复提交同一个 PATCH 是幂等，而不是冲突。"""
    topic = _create(session, admin, "same")

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"name": "same", "publish_mode": "MANUAL"},
    )

    assert changed == ()
    assert updated.name == "same"


def test_update_clears_a_list_with_an_empty_one(session: Session, admin: Actor) -> None:
    """证明提交 `[]` 与 `null` 含义相同，所以第二次 PATCH 什么都不改。"""
    topic = _create(session, admin, "cleared", keywords=["agent", "llm"])

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"keywords": []},
    )

    assert changed == ("keywords",)
    assert updated.keywords_json is None

    _, again = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"keywords": None},
    )
    assert again == ()


def test_update_rolls_the_whole_request_back_when_the_name_is_taken(
    session: Session,
    admin: Actor,
) -> None:
    """证明被拒的 PATCH 什么都不改，包括那些单看本来没问题的字段。

    改名是在循环之后才检查的，所以发现冲突时 `min_stars` 已经暂存在实例上了。
    不回滚的话，它会跟着这次拒绝的审计行一起提交，
    于是一个拿到了 `409` 的请求，反而把行改掉了。
    """
    _create(session, admin, "first")
    second = _create(session, admin, "second")

    with pytest.raises(TopicNameConflict):
        update_topic(
            session,
            actor_id=admin.user_id,
            topic_id=second.id,
            values={"name": "first", "min_stars": 999},
        )

    session.expire_all()
    reloaded = session.get(Topic, second.id)
    assert reloaded is not None
    assert reloaded.name == "second"
    assert reloaded.min_stars == 0
    failures = _rows(session, "topic.update", "failed")
    assert len(failures) == 1
    assert failures[0].resource_id == str(second.id)
    assert failures[0].metadata_json == {"name": "first", "reason": "TOPIC_NAME_CONFLICT"}


def test_update_refuses_a_blank_name(session: Session, admin: Actor) -> None:
    """证明会把主题改到没名字的 PATCH 会被拒，旧名字留下来。"""
    topic = _create(session, admin, "upd-blank")

    with pytest.raises(TopicNameBlank):
        update_topic(
            session,
            actor_id=admin.user_id,
            topic_id=topic.id,
            values={"name": "  "},
        )

    session.expire_all()
    reloaded = session.get(Topic, topic.id)
    assert reloaded is not None
    assert reloaded.name == "upd-blank"
    assert _rows(session, "topic.update", "failed")[0].resource_id == str(topic.id)


def test_update_of_a_missing_topic_is_not_found_and_unrecorded(
    session: Session,
    admin: Actor,
) -> None:
    """证明什么都不存在时也谈不上被拒绝。"""
    with pytest.raises(TopicNotFound):
        update_topic(session, actor_id=admin.user_id, topic_id=987654, values={"enabled": False})
    assert session.scalars(select(AuditLog)).all() == []


# --------------------------------------------------------------------------------------
# 删除
# --------------------------------------------------------------------------------------


def test_reference_counts_names_every_recording_table(
    session: Session,
    workspace: Workspace,
) -> None:
    """证明拒绝时的消息只有写得全才有用，而顺序也是它的一部分。

    这四个标签对应四张对 `topics.id` 有 `RESTRICT` 外键、
    并且记录主题产出了什么的表。它们是显式列出而不是自动发现的，
    这样同一次拒绝在每个部署上读起来都一样，也才值得写进契约的示例。
    """
    topic = workspace.add_topic(name="counted")
    session.commit()

    counts = reference_counts(session, topic.id)

    assert tuple(counts) == ("agent_runs", "pipeline_runs", "reports", "report_drafts")
    assert set(counts.values()) == {0}


def test_delete_refuses_a_topic_that_recorded_history(
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """证明有历史的主题是被停用而不是被删除，拒绝消息还要说明是哪几张表拦着。"""
    topic = workspace.add_topic(name="has-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    with pytest.raises(TopicInUse) as raised:
        delete_topic(session, actor_id=admin.user_id, topic_id=topic.id)

    assert raised.value.code == "TOPIC_IN_USE"
    message = str(raised.value)
    assert "agent_runs" in message
    assert "pipeline_runs" in message
    assert session.get(Topic, topic.id) is not None
    failures = _rows(session, "topic.delete", "failed")
    assert len(failures) == 1
    assert failures[0].resource_id == str(topic.id)
    assert failures[0].metadata_json["name"] == "has-history"
    assert failures[0].metadata_json["references"] == {
        "agent_runs": 1,
        "pipeline_runs": 1,
        "reports": 0,
        "report_drafts": 0,
    }


def test_delete_removes_an_unreferenced_topic_and_reports_the_links_it_took(
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """证明删主题与删它的排名是两件事，所以两个结果都要报出来。

    候选关联是一轮排名的产物而不是历史，所以跟着主题一起走。
    计数就是把这件事告诉调用方的东西，而关联行必须真的没了：
    返回 1 而那一行还在，就是一个谁都没法据以行动的数目。
    """
    topic = workspace.add_topic(name="no-history")
    source = workspace.add_source()
    repository = workspace.add_repository(source_id=source.id)
    workspace.link(topic.id, repository.id)
    session.commit()
    topic_id = topic.id

    unlinked = delete_topic(session, actor_id=admin.user_id, topic_id=topic_id)

    assert unlinked == 1
    assert session.get(Topic, topic_id) is None
    assert (
        session.scalars(select(TopicRepository).where(TopicRepository.topic_id == topic_id)).all()
        == []
    )
    success = _rows(session, "topic.delete", "success")
    assert len(success) == 1
    assert success[0].metadata_json == {"name": "no-history", "unlinked_candidate_count": 1}


def test_delete_of_a_missing_topic_is_not_found_and_unrecorded(
    session: Session,
    admin: Actor,
) -> None:
    """与更新那条一致：查不到不等于被拒绝。"""
    with pytest.raises(TopicNotFound):
        delete_topic(session, actor_id=admin.user_id, topic_id=987654)
    assert session.scalars(select(AuditLog)).all() == []
