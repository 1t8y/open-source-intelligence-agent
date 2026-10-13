"""Run T11 verifier and publishing checks against MySQL.

The script reuses the T8 seeding helpers, then drives the *real* report CLI and the real
publishing service on MySQL rather than in-memory SQLite. It exercises the three T11
acceptance criteria end to end:

1. a fabricated citation is caught by the Verifier before anything can publish;
2. a clean `AUTO_WITH_DELAY` report is released only after its withdrawal window elapses;
3. a `MANUAL` report without an approval record cannot be published at all.

The publishing clock is pinned to a fixed future moment (as in the T8 and T10 smokes) so the
payload is deterministic. The publishing service carries that pinned clock into the
`pipeline_runs` timestamps as well, otherwise `finished_at >= started_at` would be violated.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import t8_mysql_smoke as t8
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.errors import PublishPolicyBlocked
from open_source_intelligence.reports.publishing import (
    PUBLISH_MODE_MANUAL,
    publish_due_reports,
    publish_or_request_review,
    publish_report,
)
from open_source_intelligence.reports.verification import VERDICT_FAILED, VERDICT_PASSED

T11_NOW = datetime(2099, 6, 5, 12, 0, tzinfo=UTC)
FABRICATED_REF = "repo_snapshots:999999:2099-01-01"

CLI = ("-m", "open_source_intelligence.reports.cli")


def run_cli(*args: str) -> dict[str, Any]:
    """Run one real CLI invocation and parse its JSON payload."""
    completed = subprocess.run(
        [sys.executable, *CLI, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"report CLI {' '.join(args)} failed: {(completed.stderr or completed.stdout).strip()}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_cli_expecting_failure(*args: str) -> dict[str, Any]:
    """Run a CLI invocation that must fail, returning its error payload."""
    completed = subprocess.run(
        [sys.executable, *CLI, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        raise RuntimeError(f"report CLI {' '.join(args)} unexpectedly succeeded")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _refresh(session: Session) -> None:
    """Drop cached rows after the CLI wrote from another process.

    `rollback()` alone leaves already-loaded instances holding their old attribute values,
    so a later query can return a row whose ORM state no longer matches the SQL predicate.
    Production runs one short-lived session per task and is not exposed to this.
    """
    session.rollback()
    session.expire_all()


def _published_sibling(session: Session, report: Report) -> Report:
    """Insert an already-published sibling so the topic is no longer a first run."""
    sibling = Report(
        agent_run_id=report.agent_run_id,
        topic_id=report.topic_id,
        title="__T11 prior weekly report__",
        current_version=1,
        content_markdown="# prior\n",
        status="published",
        visibility="INTERNAL",
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=T11_NOW,
        published_at=T11_NOW,
    )
    session.add(sibling)
    session.commit()
    return sibling


def _verification_failure_check(session: Session, report_id: int) -> dict[str, Any]:
    """Criterion 1: the Verifier rejects an unresolvable citation, then accepts the fix."""
    source = session.scalar(
        select(ReportSource).where(ReportSource.report_id == report_id).order_by(ReportSource.id)
    )
    if source is None:
        raise RuntimeError("generated report has no citation rows")
    source_id = source.id
    original_ref = source.source_ref
    original_type = source.source_type
    original_snapshot = source.metric_snapshot_id

    source.source_type = "metric"
    source.source_ref = FABRICATED_REF
    source.metric_snapshot_id = None
    session.commit()

    failed = run_cli("--action", "verify", "--report-id", str(report_id))
    if failed["verdict"] != VERDICT_FAILED:
        raise RuntimeError(f"Verifier accepted a fabricated citation: {failed}")

    _refresh(session)
    restored_source = session.get(ReportSource, source_id)
    if restored_source is None:
        raise RuntimeError("citation row disappeared during verification check")
    restored_source.source_type = original_type
    restored_source.source_ref = original_ref
    restored_source.metric_snapshot_id = original_snapshot
    session.commit()

    passed = run_cli("--action", "verify", "--report-id", str(report_id))
    if passed["verdict"] != VERDICT_PASSED:
        raise RuntimeError(f"Verifier rejected the restored report: {passed}")
    _refresh(session)
    return {
        "failed_verdict": failed["verdict"],
        "failed_reasons": failed["reason_codes"],
        "failed_unresolved": failed["unresolved_refs"],
        "passed_verdict": passed["verdict"],
    }


def _delayed_release_check(
    session: Session,
    report_id: int,
    settings: Any,
) -> dict[str, Any]:
    """Criterion 2: a clean report waits out the window, then releases."""
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before the delayed release check")
    _published_sibling(session, report)

    _refresh(session)
    decision = publish_or_request_review(
        session,
        report_id=report_id,
        settings=settings,
        now=T11_NOW,
    )
    early = publish_due_reports(session, now=T11_NOW)
    late = publish_due_reports(session, now=T11_NOW + timedelta(minutes=61))
    session.expire_all()
    released = session.get(Report, report_id)
    if released is None:
        raise RuntimeError("report vanished after the delayed release check")
    matched = [entry for entry in late if entry["report_id"] == report_id]
    window_minutes = settings.publish_delay_minutes
    return {
        "decision_status": decision.status,
        "decision_requires_review": decision.requires_review,
        "decision_publish_at": decision.publish_at.isoformat() if decision.publish_at else None,
        "expected_publish_at": (T11_NOW + timedelta(minutes=window_minutes)).isoformat(),
        "window_minutes": window_minutes,
        "early_release_count": len(early),
        "late_release_count": len(late),
        "late_released_status": matched[0]["status"] if matched else None,
        "final_status": released.status,
        "final_published_at": released.published_at.isoformat() if released.published_at else None,
    }


def _manual_approval_check(
    session: Session,
    report_id: int,
    settings: Any,
    reviewer_id: int,
) -> dict[str, Any]:
    """Criterion 3: `MANUAL` cannot publish without an approval record."""
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before the manual approval check")
    report.status = "draft"
    report.publish_at = None
    report.published_at = None
    report.publish_mode = PUBLISH_MODE_MANUAL
    report.visibility = "INTERNAL"
    session.commit()

    review_request = run_cli("--action", "publish", "--report-id", str(report_id))

    _refresh(session)
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished after requesting review")
    report.status = "scheduled"
    report.publish_at = T11_NOW - timedelta(minutes=1)
    session.commit()

    blocked_code = ""
    try:
        publish_report(session, report_id=report_id, now=T11_NOW)
    except PublishPolicyBlocked as exc:
        blocked_code = exc.code
    _refresh(session)

    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished while restoring the review state")
    report.status = "pending_review"
    report.publish_at = None
    session.commit()

    approved = run_cli(
        "--action",
        "review",
        "--report-id",
        str(report_id),
        "--decision",
        "approve",
        "--reviewer-id",
        str(reviewer_id),
    )
    _refresh(session)
    due = publish_due_reports(session, now=T11_NOW)
    session.expire_all()
    final = session.get(Report, report_id)
    if final is None:
        raise RuntimeError("report vanished after approval")
    matched = [entry for entry in due if entry["report_id"] == report_id]
    return {
        "review_request_status": review_request["status"],
        "review_request_requires_review": review_request["requires_review"],
        "blocked_code": blocked_code,
        "approved_status": approved["status"],
        "released_count": len(matched),
        "released_status": matched[0]["status"] if matched else None,
        "final_status": final.status,
        "final_published_at": final.published_at.isoformat() if final.published_at else None,
    }


def run_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """Drive the real Agent, CLI, and publishing service through the T11 path."""
    settings = get_settings()
    agent_result = run_research_agent(
        session,
        topic_id=ids["topic_id"],
        settings=settings,
        now=t8.SMOKE_NOW,
    )
    if agent_result.report_draft_id is None:
        raise RuntimeError("Agent run did not create a report draft")

    generated = run_cli("--agent-run-id", str(agent_result.agent_run_id))
    report_id = int(generated["report_id"])
    _refresh(session)

    reviewer_id = session.scalar(select(User.id).where(User.username == t8.VERIFIER_USERNAME))
    if reviewer_id is None:
        raise RuntimeError("T8 verifier user was not seeded")

    verification = _verification_failure_check(session, report_id)
    delayed = _delayed_release_check(session, report_id, settings)
    manual = _manual_approval_check(session, report_id, settings, int(reviewer_id))

    session.expire_all()
    verification_rows = len(
        session.scalars(
            select(ReportVerification).where(ReportVerification.report_id == report_id)
        ).all()
    )
    review_rows = len(
        session.scalars(select(ReportReview).where(ReportReview.report_id == report_id)).all()
    )
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before summarizing")

    return {
        "agent_status": agent_result.status,
        "report_id": report_id,
        "report_status": report.status,
        "report_version": report.current_version,
        "verification_rows": verification_rows,
        "review_rows": review_rows,
        "verification": verification,
        "delayed": delayed,
        "manual": manual,
    }


def _cleanup(session: Session) -> None:
    """Remove T11 verdict and review rows, then the T8 records they hang off."""
    report_ids = select(Report.id).where(Report.title.like("__T11%"))
    session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
    session.execute(delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids)))
    session.execute(delete(Report).where(Report.title.like("__T11%")))
    session.commit()
    t8.cleanup_verification_records(session)


def main() -> None:
    """Run T11 MySQL smoke checks and print the result payload."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            _cleanup(session)
            ids = t8.seed_topics(session)
            payload = run_checks(session, ids)
    finally:
        with session_factory() as session:
            _cleanup(session)
        engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
