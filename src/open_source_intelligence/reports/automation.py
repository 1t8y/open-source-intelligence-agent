"""Carrying a finished research run the rest of the way, without an operator.

Until this module the daily chain stopped at a draft. `reports/cli.py --action generate` turned a
draft into a report, but nothing called it on a schedule, so an unattended day ended with
`report_drafts` rows and no `reports` row - which meant nothing to verify, nothing to publish, and
no email. The claim "每天早上一封周报" was true only if a human typed three commands.

`finish_run_report` is that missing edge, kept out of the worker task so it can be tested without
Celery and reused by a CLI. It does not weaken any gate: the report still has to compose (every
numbered section needs a resolvable metric citation), still has to pass verification, and still
goes through the publish policy - a run whose evidence was thin comes back as `pending_review`,
not as a published report.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import ReportGenerationError
from open_source_intelligence.reports.publishing import publish_or_request_review
from open_source_intelligence.reports.verification import verify_report


def finish_run_report(session: Session, *, agent_run_id: int) -> dict[str, Any]:
    """Compose a run's draft into a report, verify it, and apply the publish policy.

    Returns a small JSON-ready summary rather than raising: a run whose draft does not compose is
    a normal outcome of a thin evidence base, and the caller (a Celery task) has to report it
    without failing the task and triggering a retry that would spend model budget again.
    """
    try:
        generated = generate_report(session, agent_run_id=agent_run_id)
        session.commit()
    except ReportGenerationError as exc:
        session.rollback()
        return {
            "stage": "generate",
            "report_id": None,
            "error_code": getattr(exc, "code", None),
            "error_message": str(exc),
        }

    verification = verify_report(session, report_id=generated.report_id)
    session.commit()
    summary: dict[str, Any] = {
        "stage": "verify",
        "report_id": generated.report_id,
        "verdict": verification.verdict,
        "reason_codes": list(verification.reason_codes),
    }
    if verification.verdict != "passed":
        # The report exists and a human can now look at it; publishing is the policy's call and
        # a failed verification is exactly what the review queue is for.
        return summary

    outcome = publish_or_request_review(session, report_id=generated.report_id)
    session.commit()
    summary.update(
        {
            "stage": "publish",
            "status": outcome.status,
            "mode": outcome.mode,
            "requires_review": outcome.requires_review,
            "publish_at": outcome.publish_at.isoformat() if outcome.publish_at else None,
            "reason_codes": list(outcome.reason_codes),
        }
    )
    return summary
