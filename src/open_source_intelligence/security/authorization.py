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
