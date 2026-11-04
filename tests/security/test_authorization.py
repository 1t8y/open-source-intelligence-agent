"""直接守护报告授权这一处中心化判定。

`docs/security-and-privacy.md` §3.1.1 要求判定只存在一个地方，
`tests/api/test_reports.py` 里的 HTTP 用例证明各端点遵守了它。
这里的用例钉住的是判定*本身*，因为有两处会在任何端点都不报错的情况下坏掉：

* `can_read_report` 与 `readable_reports_filter` 是同一条规则的两种表达 ——
  一个 Python 谓词、一个 SQL 谓词。两者一旦漂移，详情页与列表查询就会
  各说各话，而这种分歧只会表现为一个需要有人注意到的计数偏差。
* 角色闸门与可见性闸门是刻意分层的：`require_roles` 回答
  「这个账号能不能读报告」，`can_read_report` 回答「能不能读*这一份*」。
  把两者合并会悄悄放宽 `VIEWER`，所以这里把拆分断言出来，而不是留在暗处。

主题规则用更少的活动部件重复了同一形状 —— 一个 `ADMIN` 直通
加一个 `enabled` 标志 —— 文件末尾对它做了同样的处理，理由也一样：
它有一个 Python 表达式和一个 SQL 表达式，而主题列表的 `total` 正依赖这一对。
"""

from __future__ import annotations

import pytest
from api_support import Actor, create_actor
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.security.authorization import (
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_VIEWER,
    authorize_report_read,
    authorize_topic_read,
    can_read_report,
    can_read_topic,
    readable_reports_filter,
    readable_topics_filter,
    require_report_read,
)

TITLES = {
    "internal": "__internal__",
    "team": "__team__",
    "private_own": "__private_own__",
    "private_other": "__private_other__",
    "private_orphan": "__private_orphan__",
}

#: 部署可能产生的每一种（角色, 数据范围）组合。`VIEWER` 永远拿不到 `team`，
#: 但这里仍然包含该组合：可见性规则不能依赖一个它从不查询的角色。
PRINCIPALS = (
    (ROLE_ADMIN, "personal"),
    (ROLE_ADMIN, "team"),
    (ROLE_EDITOR, "personal"),
    (ROLE_EDITOR, "team"),
    (ROLE_VIEWER, "personal"),
    (ROLE_VIEWER, "team"),
)


def _request(path: str = "/api/v1/reports/1") -> Request:
    """构造一个最小的 ASGI 请求，够审计关联与客户端 IP 使用即可。"""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 45678),
            "state": {},
        }
    )


def _detached(actor: Actor, data_scope: str) -> User:
    """给谓词用例返回一个未附着到会话的用户行。

    刻意做成游离对象：`can_read_report` 与 `readable_reports_filter` 只读属性，
    把一个临时行交给它们，既覆盖了规则，又不会改动种子操作者的数据范围。
    """
    return User(
        id=actor.user_id,
        username=actor.username,
        password_hash="unused",
        display_name=actor.username,
        data_scope=data_scope,
    )


def _stored(session: Session, actor: Actor) -> User:
    """重新读出一个操作者的用户行，带上审计路径需要的角色关联。"""
    user = session.get(User, actor.user_id)
    assert user is not None
    return user


@pytest.fixture()
def editor(session: Session) -> Actor:
    """被测的主体：一个可以读 TEAM 报告的编辑者。"""
    return create_actor(session, username="authz-editor", role=ROLE_EDITOR, data_scope="team")


@pytest.fixture()
def outsider(session: Session) -> Actor:
    """第二个编辑者，个人范围，拥有其中一份 PRIVATE 报告。"""
    return create_actor(session, username="authz-outsider", role=ROLE_EDITOR)


@pytest.fixture()
def reports(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    outsider: Actor,
) -> dict[str, Report]:
    """灌入五份报告，覆盖所有要紧的可见性 / 归属组合。"""
    topic = workspace.add_topic(name="authz")
    agent_run = workspace.add_agent_run(topic.id)

    def add(title: str, visibility: str, owner_id: int | None) -> Report:
        report = workspace.add_report(
            topic_id=topic.id,
            agent_run_id=agent_run.id,
            title=title,
            status="published",
        )
        report.visibility = visibility
        report.owner_id = owner_id
        session.flush()
        return report

    seeded = {
        "internal": add(TITLES["internal"], "INTERNAL", None),
        "team": add(TITLES["team"], "TEAM", None),
        "private_own": add(TITLES["private_own"], "PRIVATE", editor.user_id),
        "private_other": add(TITLES["private_other"], "PRIVATE", outsider.user_id),
        "private_orphan": add(TITLES["private_orphan"], "PRIVATE", None),
    }
    session.commit()
    return seeded


def _expected(role: str, data_scope: str) -> set[str]:
    """独立于被测实现，把可见集合写出来。

    `private_own` 与角色无关：归属是拿行本身核对的，而不是看账号有什么权限，
    所以哪怕是 `VIEWER`，也能读到属于自己的那份 PRIVATE 报告。
    """
    if role == ROLE_ADMIN:
        return set(TITLES)
    visible = {"internal", "private_own"}
    if data_scope == "team":
        visible.add("team")
    return visible


@pytest.mark.parametrize(("role", "data_scope"), PRINCIPALS)
def test_can_read_report_follows_visibility_and_ownership(
    reports: dict[str, Report],
    editor: Actor,
    role: str,
    data_scope: str,
) -> None:
    """证明一个主体读到的，恰好是它可见范围内那几份报告。"""
    user = _detached(editor, data_scope)
    seen = {name for name, report in reports.items() if can_read_report(user, report, {role})}
    assert seen == _expected(role, data_scope)


@pytest.mark.parametrize(("role", "data_scope"), PRINCIPALS)
def test_readable_reports_filter_agrees_with_can_read_report(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
    role: str,
    data_scope: str,
) -> None:
    """证明 SQL 谓词与 Python 谓词选中的是同一批行。

    只比集合相等的话，两边以同一种方式错也会通过，所以这里同时断言了预期集合。
    """
    user = _detached(editor, data_scope)
    roles = {role}
    name_of = {report.id: name for name, report in reports.items()}

    by_python = {name for name, report in reports.items() if can_read_report(user, report, roles)}
    by_sql = {
        name_of[report_id]
        for report_id in session.scalars(
            select(Report.id).where(readable_reports_filter(user, roles))
        ).all()
    }

    assert by_sql == by_python
    assert by_sql == _expected(role, data_scope)


def test_admin_bypass_keeps_the_ownerless_private_row_readable(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明 `ADMIN` 在可见性之前短路，因此没有归属的 PRIVATE 行依然可读。

    对一行从未设置过归属的数据来说，这就是对「私有」的刻意解读：
    少了管理员直通，这一行会对所有人都不可读，那是把门焊死，而不是保护。
    """
    orphan = reports["private_orphan"]
    assert can_read_report(_detached(editor, "personal"), orphan, {ROLE_ADMIN}) is True
    assert can_read_report(_detached(editor, "personal"), orphan, {ROLE_EDITOR}) is False


def test_visibility_gate_never_consults_the_role(
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明 `can_read_report` 只回答「读*哪一份*」，
    从不回答「这个账号能不能读」。

    第二个问题由先运行的 `require_roles(*REPORT_READ_ROLES)` 负责。
    因此 `VIEWER` 对一份 `INTERNAL` 报告也能通过 `can_read_report`：
    拦下无权限调用方的是角色依赖，不是这个谓词。
    """
    user = _detached(editor, "personal")
    assert can_read_report(user, reports["internal"], {ROLE_VIEWER}) is True
    assert can_read_report(user, reports["internal"], set()) is True
    assert can_read_report(user, reports["team"], {ROLE_VIEWER}) is False


def test_require_report_read_records_the_denied_resource(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明拒绝是记在被访问的那份报告上，而不只是记在操作者身上。"""
    report = reports["private_other"]
    with pytest.raises(HTTPException) as raised:
        require_report_read(session, _request(), _stored(session, editor), report)

    assert raised.value.status_code == 403
    denial = session.scalar(select(AuditLog).where(AuditLog.action == "report.read"))
    assert denial is not None
    assert denial.result == "denied"
    assert denial.resource_type == "report"
    assert denial.resource_id == str(report.id)
    assert denial.actor_id == editor.user_id
    assert denial.ip_address is not None
    assert denial.request_id == "unknown"


def test_require_report_read_is_silent_when_it_allows(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明一次允许的读取不写审计行：轨迹记的是拒绝，不是流量。"""
    require_report_read(session, _request(), _stored(session, editor), reports["team"])
    assert session.scalar(select(AuditLog).where(AuditLog.action == "report.read")) is None


def test_authorize_report_read_denial_carries_no_report_metadata(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明 403 不会透露调用方看都不能看的那份报告的任何信息。"""
    report = reports["private_other"]
    with pytest.raises(HTTPException) as raised:
        authorize_report_read(
            session,
            _request(),
            _stored(session, editor),
            report_id=report.id,
        )
    assert raised.value.status_code == 403
    assert TITLES["private_other"] not in str(raised.value.detail)
    assert str(report.id) not in str(raised.value.detail)


def test_authorize_report_read_maps_a_miss_to_404_without_auditing(
    session: Session,
    editor: Actor,
) -> None:
    """证明查不到时是 404 且不写审计：资源从未存在，也就没有拒绝可言。"""
    with pytest.raises(HTTPException) as raised:
        authorize_report_read(
            session,
            _request(),
            _stored(session, editor),
            report_id=987654321,
        )
    assert raised.value.status_code == 404
    assert session.scalar(select(AuditLog).where(AuditLog.action == "report.read")) is None


def test_authorize_report_read_returns_the_row_when_allowed(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """证明获得许可的读取直接把那一行交回来。"""
    expected = reports["private_own"]
    returned = authorize_report_read(
        session,
        _request(),
        _stored(session, editor),
        report_id=expected.id,
    )
    assert returned.id == expected.id


# --------------------------------------------------------------------------------------
# 主题规则：同样的成对表达，作用在只有一个标志、没有范围概念的资源上
# --------------------------------------------------------------------------------------

#: 只看角色。`can_read_topic` 从不查询 `data_scope`，这正是下面那条规则的要点。
TOPIC_PRINCIPALS = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)


@pytest.fixture()
def topics(session: Session, workspace: Workspace) -> dict[str, Topic]:
    """一个启用主题和一个停用主题：主题可见性规则的全部内容。"""
    enabled = workspace.add_topic(name="authz-topic-enabled", enabled=True)
    disabled = workspace.add_topic(name="authz-topic-disabled", enabled=False)
    session.commit()
    return {"enabled": enabled, "disabled": disabled}


@pytest.mark.parametrize("role", TOPIC_PRINCIPALS)
def test_readable_topics_filter_agrees_with_can_read_topic(
    session: Session,
    topics: dict[str, Topic],
    editor: Actor,
    role: str,
) -> None:
    """证明 SQL 谓词与 Python 谓词选中的是同一批行。

    主题列表的 `total` 用谓词算出来，单条读取走函数。两者一旦不一致，
    调用方可能先被告知有两个主题，接着又被拒掉其中一个 ——
    反过来更糟：被告知只有一个，而详情读取却让它看到两个。
    只比集合相等的话，两边以同一种方式错也会通过，所以这里同时断言了预期集合。
    """
    user = _detached(editor, "personal")
    roles = {role}
    name_of = {topic.id: name for name, topic in topics.items()}

    by_python = {name for name, topic in topics.items() if can_read_topic(user, topic, roles)}
    by_sql = {
        name_of[topic_id]
        for topic_id in session.scalars(
            select(Topic.id).where(readable_topics_filter(user, roles))
        ).all()
    }

    assert by_sql == by_python
    assert by_sql == (set(topics) if role == ROLE_ADMIN else {"enabled"})


def test_the_topic_rule_never_consults_the_data_scope(
    topics: dict[str, Topic],
    editor: Actor,
) -> None:
    """证明停用主题是未发布的配置，而不是别人的私有数据。

    这里的闸门是角色检查而非归属检查，所以放宽账号的数据范围不会改变任何结果。
    把这一点断言出来，后来的人就不能靠「让它长得像报告那条规则」来*修*主题规则。
    """
    for scope in ("personal", "team"):
        user = _detached(editor, scope)
        assert can_read_topic(user, topics["enabled"], {ROLE_EDITOR}) is True
        assert can_read_topic(user, topics["disabled"], {ROLE_EDITOR}) is False
        assert can_read_topic(user, topics["disabled"], {ROLE_ADMIN}) is True


def test_authorize_topic_read_records_the_topic_it_refused(
    session: Session,
    topics: dict[str, Topic],
    editor: Actor,
) -> None:
    """证明看不见的主题返回的是 `403`：响应体不说，审计行却说。"""
    with pytest.raises(HTTPException) as raised:
        authorize_topic_read(
            session,
            _request("/api/v1/topics/1"),
            _stored(session, editor),
            topic_id=topics["disabled"].id,
            action="topic.read",
        )

    assert raised.value.status_code == 403
    assert str(topics["disabled"].id) not in str(raised.value.detail)
    denial = session.scalar(select(AuditLog).where(AuditLog.action == "topic.read"))
    assert denial is not None
    assert denial.result == "denied"
    assert denial.resource_type == "topic"
    assert denial.resource_id == str(topics["disabled"].id)
    assert denial.actor_id == editor.user_id


def test_authorize_topic_read_keeps_the_catalog_action_by_default(
    session: Session,
    topics: dict[str, Topic],
    editor: Actor,
) -> None:
    """证明仓库目录按主题过滤时，记下的仍然是 `repository.read`。

    `action` 默认沿用更早那个调用方的名字是刻意的：
    一个沿用别人请求名的界面，产出的轨迹是读不回来的。
    """
    with pytest.raises(HTTPException):
        authorize_topic_read(
            session,
            _request("/api/v1/repositories"),
            _stored(session, editor),
            topic_id=topics["disabled"].id,
        )

    assert session.scalar(select(AuditLog).where(AuditLog.action == "repository.read")) is not None
    assert session.scalar(select(AuditLog).where(AuditLog.action == "topic.read")) is None


def test_authorize_topic_read_maps_a_miss_to_404_without_auditing(
    session: Session,
    editor: Actor,
) -> None:
    """证明从未存在过的主题不算被拒绝，因此什么都不记。"""
    with pytest.raises(HTTPException) as raised:
        authorize_topic_read(
            session,
            _request("/api/v1/topics/987654321"),
            _stored(session, editor),
            topic_id=987654321,
        )

    assert raised.value.status_code == 404
    assert session.scalars(select(AuditLog)).all() == []


def test_authorize_topic_read_returns_the_row_when_allowed(
    session: Session,
    topics: dict[str, Topic],
    editor: Actor,
) -> None:
    """证明获得许可的读取直接把那一行交回来，路由正是拿它做序列化。"""
    returned = authorize_topic_read(
        session,
        _request("/api/v1/topics/1"),
        _stored(session, editor),
        topic_id=topics["enabled"].id,
        action="topic.read",
    )
    assert returned.id == topics["enabled"].id
