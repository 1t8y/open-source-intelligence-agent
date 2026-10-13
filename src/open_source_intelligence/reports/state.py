"""The report publishing state machine.

`reports.status` is written from three places — the publish policy engine, the manual
revision service, and report composition — so the allowed edges live in their own module.
Keeping them here also avoids a circular import: `publishing` depends on `verification`,
which depends on `composition`, so `composition` cannot import `publishing` back.
"""

from __future__ import annotations

from typing import Final

from open_source_intelligence.persistence.models.reporting import Report

from .errors import ReportStateError

#: Legal report-status transitions. `failed` is terminal; `published` can only be pulled
#: back to `draft`, which is how a withdrawal or a rollback returns a report to the queue.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"pending_review", "scheduled", "failed"}),
    "pending_review": frozenset({"approved", "changes_requested", "failed"}),
    "approved": frozenset({"scheduled", "failed"}),
    "scheduled": frozenset({"published", "failed"}),
    "changes_requested": frozenset({"draft"}),
    "published": frozenset({"draft"}),
    "failed": frozenset(),
}


def transition_report(report: Report, target: str) -> None:
    """Move a report to `target`, rejecting any edge the state machine forbids."""
    if report.status == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(report.status, frozenset())
    if target not in allowed:
        raise ReportStateError(f"report {report.id} cannot move from {report.status} to {target}")
    report.status = target
