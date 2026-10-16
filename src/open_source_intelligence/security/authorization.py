"""Report authorization: one place that decides who may read, edit, review, and publish.

`docs/security-and-privacy.md` §3.3 requires the permission decision to be centralized
rather than scattered across routes, so both the request-time guard and the list-query
predicate come from here.

The initial model is deliberately small:

* `ADMIN` reads every report.
* `INTERNAL` visibility is readable by any authenticated user.
* `TEAM` visibility additionally requires `data_scope = team`.
* `PRIVATE` visibility requires being the recorded owner. A report with no owner stays
  `ADMIN`-only, which is the safe reading of "private" for a row whose owner was never set.

`readable_reports_filter` expresses the same rule as a SQL predicate, so a list query can
push the decision into the database instead of fetching rows and filtering afterwards.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, Request, status
from sqlalchemy import ColumnElement, and_, or_, true
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.security.dependencies import deny_request
from open_source_intelligence.security.service import get_role_codes

ROLE_ADMIN: Final[str] = "ADMIN"
ROLE_EDITOR: Final[str] = "EDITOR"
ROLE_VIEWER: Final[str] = "VIEWER"

#: Roles allowed to write: verify, edit, review, publish, and roll back.
REPORT_WRITE_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR)
#: Roles allowed to read reports at all.
REPORT_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: Roles allowed to read the operational overview and the Agent run ledger. These surfaces
#: expose cross-topic cost, failure reasons and evaluation verdicts, so they sit with the
#: operator roles rather than with report readers - a viewer sees reports, not the ledger that
#: produced them.
METRICS_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR)

#: Roles allowed to read the repository catalog. The catalog holds public repository metadata
#: (`docs/security-and-privacy.md` §2 classifies it as public), so every authenticated role
#: reads it; what needs adjudicating is not the catalog but whose topics it may be sliced by,
#: which is what `can_read_topic` decides.
REPOSITORY_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: Roles allowed to read the audit log. Reading it is itself a privilege: the entries name
#: every refused request, every configuration change and every publish on the platform.
AUDIT_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

#: Roles allowed to see the topic list at all. This is only the gate that keeps anonymous callers
#: out; *which* topics a caller sees is decided by `can_read_topic`, because a disabled topic is
#: unpublished configuration rather than someone else's private data. The set matches the report
#: and catalog read sets: reading a topic is no more privileged than reading a report.
TOPIC_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: Roles allowed to change a topic. `EDITOR`'s write permission covers reports and nothing else,
#: and a topic is not a report: it decides which repositories get collected, which fixes the model
#: budget and the public-API quota the next daily run will spend. That is the same reason
#: `docs/api-contract.md` §3.1 refuses a `POST /sources` - an endpoint that can move a rate-limit
#: budget hands usage control to any authenticated client.
TOPIC_WRITE_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

TEAM_DATA_SCOPE: Final[str] = "team"

VISIBILITY_PRIVATE: Final[str] = "PRIVATE"
VISIBILITY_TEAM: Final[str] = "TEAM"
VISIBILITY_INTERNAL: Final[str] = "INTERNAL"


def can_read_report(user: User, report: Report, roles: set[str]) -> bool:
    """Return whether `user` may read `report`."""
    if ROLE_ADMIN in roles:
        return True
    if report.visibility == VISIBILITY_INTERNAL:
        return True
    if report.visibility == VISIBILITY_TEAM:
        return user.data_scope == TEAM_DATA_SCOPE
    return report.owner_id is not None and report.owner_id == user.id


def readable_reports_filter(user: User, roles: set[str]) -> ColumnElement[bool]:
    """Return the access rule as a SQL predicate for report list queries."""
    if ROLE_ADMIN in roles:
        return true()
    conditions: list[ColumnElement[bool]] = [Report.visibility == VISIBILITY_INTERNAL]
    if user.data_scope == TEAM_DATA_SCOPE:
        conditions.append(Report.visibility == VISIBILITY_TEAM)
    conditions.append(and_(Report.visibility == VISIBILITY_PRIVATE, Report.owner_id == user.id))
    return or_(*conditions)


def require_report_read(
    session: Session,
    request: Request,
    user: User,
    report: Report,
) -> None:
    """Deny and audit when the current user may not read the report.

    The 403 body carries no report metadata, so a denied request learns nothing about a
    report it may not see. The audit row is the mirror image: it *does* name the report, since
    an internal trail that cannot say which resource was refused cannot answer "who tried to
    read what", which is the only question a denial record exists to answer.
    """
    if can_read_report(user, report, get_role_codes(user)):
        return
    deny_request(
        session,
        request,
        action="report.read",
        resource_type="report",
        resource_id=str(report.id),
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient permissions for this report",
        actor_id=user.id,
    )


def authorize_report_read(
    session: Session,
    request: Request,
    user: User,
    *,
    report_id: int,
) -> Report:
    """Load one report for reading, enforcing the ACL and mapping failures to HTTP codes."""
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"report {report_id} does not exist",
        )
    require_report_read(session, request, user, report)
    return report


def can_read_topic(user: User, topic: Topic, roles: set[str]) -> bool:
    """Return whether `user` may slice the repository catalog by `topic`.

    The catalog itself is public, so this is not a container ACL. It guards the membership
    question: which repositories a topic tracks is internal strategy, and `topic_repositories`
    is where that becomes observable.

    `ADMIN` reads every topic. Everyone else reads `enabled` topics — that is the whole of the
    rule, deliberately. `topics` has no ACL column, and inventing a per-topic grant would be a
    new feature (`REQUIREMENTS.md` puts full ABAC out of scope), not documentation sync. A
    disabled topic is treated as unpublished configuration rather than as someone else's
    private data, which is why the gate is a role check and not an ownership check.
    """
    if ROLE_ADMIN in roles:
        return True
    return topic.enabled


def readable_topics_filter(user: User, roles: set[str]) -> ColumnElement[bool]:
    """Return the topic visibility rule as a SQL predicate for topic list queries.

    The predicate form of `can_read_topic`, and it has to remain the predicate form of it. The
    contract states two different-looking requirements that are the same requirement: the rule
    must not be reimplemented per surface, and the list's `total` must be computed under it - a
    query that fetched every topic and dropped the invisible ones afterwards would still report a
    count of topics the caller may not see. `tests/security/test_authorization.py` compares this
    predicate against `can_read_topic` row by row rather than trusting the pair to be edited
    together.
    """
    if ROLE_ADMIN in roles:
        return true()
    return Topic.enabled.is_(True)


def authorize_topic_read(
    session: Session,
    request: Request,
    user: User,
    *,
    topic_id: int,
    action: str = "repository.read",
) -> Topic:
    """Load one topic for catalog filtering, or refuse the request.

    The failure split follows the report resource: a topic that does not exist is `404`, and a
    topic that exists but is not visible is `403`. The `403` body carries no topic metadata and
    the audit row names the topic — the same mirror the report ACL uses, for the same reason.

    `action` is a parameter because one rule has two surfaces: the repository catalog filters by
    topic and records `repository.read`, while the topic endpoint itself records `topic.read`. The
    default keeps the older caller's behaviour, so a second surface names its own action instead of
    inheriting a name that describes a different request.
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"topic {topic_id} does not exist",
        )
    if can_read_topic(user, topic, get_role_codes(user)):
        return topic
    deny_request(
        session,
        request,
        action=action,
        resource_type="topic",
        resource_id=str(topic.id),
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient permissions for this topic",
        actor_id=user.id,
    )
