"""Direct tests for the central report-authorization decision.

`docs/security-and-privacy.md` §3.1.1 requires the decision to live in one place, and the
HTTP tests in `tests/api/test_reports.py` prove the endpoints honour it. These tests pin the
decision *itself*, because two things can break without any endpoint failing:

* `can_read_report` and `readable_reports_filter` are two expressions of one rule — a Python
  predicate and a SQL predicate. If they drift, the detail page and the list query disagree,
  and the disagreement only shows up as a miscount somebody has to notice.
* The role gate and the visibility gate are layered on purpose: `require_roles` answers "may
  this account read reports at all", `can_read_report` answers "may it read *this* one".
  Collapsing the two would silently widen `VIEWER`, so the split is asserted here rather than
  left implicit.

The topic rule repeats the shape with fewer moving parts - an `ADMIN` bypass plus an `enabled`
flag - and gets the same treatment at the end of the file, for the same reason: it has one Python
expression and one SQL expression, and the pair is what the topic list's `total` depends on.
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

#: Every (role, data scope) pair the deployment can produce. `VIEWER` never gets `team`, but the
#: combination is included: the visibility rule must not depend on a role it never consults.
PRINCIPALS = (
    (ROLE_ADMIN, "personal"),
    (ROLE_ADMIN, "team"),
    (ROLE_EDITOR, "personal"),
    (ROLE_EDITOR, "team"),
    (ROLE_VIEWER, "personal"),
    (ROLE_VIEWER, "team"),
)


def _request(path: str = "/api/v1/reports/1") -> Request:
    """Build a minimal ASGI request, enough for audit correlation and client IP."""
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
    """Return an unattached user row for the predicate tests.

    Detached on purpose: `can_read_report` and `readable_reports_filter` read attributes, so
    handing them a transient row exercises the rule without mutating the seeded actor's scope.
    """
    return User(
        id=actor.user_id,
        username=actor.username,
        password_hash="unused",
        display_name=actor.username,
        data_scope=data_scope,
    )


def _stored(session: Session, actor: Actor) -> User:
    """Reload one actor's user row, with the role links the audit path needs."""
    user = session.get(User, actor.user_id)
    assert user is not None
    return user


@pytest.fixture()
def editor(session: Session) -> Actor:
    """The principal under test: an editor who may read TEAM reports."""
    return create_actor(session, username="authz-editor", role=ROLE_EDITOR, data_scope="team")


@pytest.fixture()
def outsider(session: Session) -> Actor:
    """A second editor, personal scope, who owns one of the PRIVATE reports."""
    return create_actor(session, username="authz-outsider", role=ROLE_EDITOR)


@pytest.fixture()
def reports(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    outsider: Actor,
) -> dict[str, Report]:
    """Seed five reports covering every visibility/owner combination that matters."""
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
    """Spell out the visible set independently of the implementation under test.

    `private_own` is unconditional on the role: ownership is checked against the row, not
    against what the account may do, so even a `VIEWER` reads the PRIVATE report it owns.
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
    """One principal reads exactly the reports its visibility scope allows."""
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
    """The SQL predicate and the Python predicate select the same rows.

    Set equality alone would pass if both were wrong in the same way, so the expected set is
    asserted alongside it.
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
    """`ADMIN` short-circuits before visibility, so a PRIVATE row with no owner stays readable.

    That is the deliberate reading of "private" for a row whose owner was never set: without
    the admin bypass the row would be unreadable by everyone, which is a lockout rather than a
    protection.
    """
    orphan = reports["private_orphan"]
    assert can_read_report(_detached(editor, "personal"), orphan, {ROLE_ADMIN}) is True
    assert can_read_report(_detached(editor, "personal"), orphan, {ROLE_EDITOR}) is False


def test_visibility_gate_never_consults_the_role(
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """`can_read_report` answers *which* report, never *whether an account may read at all*.

    The second question is `require_roles(*REPORT_READ_ROLES)`, which runs first. A `VIEWER`
    therefore passes `can_read_report` for an `INTERNAL` report: what stops an unprivileged
    caller is the role dependency, not this predicate.
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
    """A denial is audited against the report that was addressed, not just the actor."""
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
    """An allowed read writes no audit row: the trail records refusals, not traffic."""
    require_report_read(session, _request(), _stored(session, editor), reports["team"])
    assert session.scalar(select(AuditLog).where(AuditLog.action == "report.read")) is None


def test_authorize_report_read_denial_carries_no_report_metadata(
    session: Session,
    reports: dict[str, Report],
    editor: Actor,
) -> None:
    """The 403 says nothing about a report the caller may not see."""
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
    """A miss is a 404 and audits nothing: the resource never existed, so nothing was denied."""
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
    """A permitted read hands back the row itself."""
    expected = reports["private_own"]
    returned = authorize_report_read(
        session,
        _request(),
        _stored(session, editor),
        report_id=expected.id,
    )
    assert returned.id == expected.id


# --------------------------------------------------------------------------------------
# The topic rule: the same pair, on a resource with one flag instead of a scope
# --------------------------------------------------------------------------------------

#: Roles only. `can_read_topic` never consults `data_scope`, which is the point of the rule below.
TOPIC_PRINCIPALS = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)


@pytest.fixture()
def topics(session: Session, workspace: Workspace) -> dict[str, Topic]:
    """One enabled topic and one disabled one: the whole of the topic visibility rule."""
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
    """The SQL predicate and the Python predicate select the same rows.

    The topic list's `total` is computed with the predicate, and the single read uses the function.
    If they disagreed, a caller could be told there are two topics and then be refused one of them
    - or, worse in the other direction, be told there is one while the detail read lets it see two.
    Set equality alone would pass if both were wrong the same way, so the expected set is asserted
    alongside it.
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
    """A disabled topic is unpublished configuration, not somebody else's private data.

    The gate is a role check rather than an ownership check, so widening an account's scope changes
    nothing here. Asserting it stops a later edit from "fixing" the topic rule by making it look
    like the report one.
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
    """An invisible topic is a `403` whose body is silent and whose audit row is not."""
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
    """The repository catalog filters by topic and still records `repository.read`.

    `action` defaults to the older caller's name on purpose: a surface that inherits a name
    describing a different request produces a trail that cannot be read back.
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
    """A topic that never existed was not refused, so nothing is recorded."""
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
    """A permitted read hands back the row itself, which is what the route serialises."""
    returned = authorize_topic_read(
        session,
        _request("/api/v1/topics/1"),
        _stored(session, editor),
        topic_id=topics["enabled"].id,
        action="topic.read",
    )
    assert returned.id == topics["enabled"].id
