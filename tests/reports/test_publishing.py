"""Tests for the risk-based publish policy and the report state machine.

The state machine declared in `INTERFACES.md` §8 is the contract under test:

```text
draft -> pending_review -> approved -> scheduled -> published
```

with `failed` terminal and `changes_requested` as the return branch. Policy must only ever
tighten the configured `publish_mode`, never loosen it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from reports_support import (
    AUTO,
    AUTO_WITH_DELAY,
    MANUAL,
    SeededReport,
    build_report,
    seed_published_sibling,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
    ReportVersion,
)
from open_source_intelligence.pipeline.runs import list_pipeline_runs
from open_source_intelligence.reports.errors import (
    PublishPolicyBlocked,
    ReportGenerationError,
    ReportStateError,
)
from open_source_intelligence.reports.publishing import (
    PUBLISH_MODE_AUTO,
    PUBLISH_MODE_AUTO_WITH_DELAY,
    PUBLISH_MODE_MANUAL,
    REASON_FIRST_RUN,
    REASON_NOT_INTERNAL_VISIBILITY,
    REASON_SENSITIVE_REPORT,
    REASON_VERIFICATION_FAILED,
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_REQUEST_CHANGES,
    evaluate_publish_policy,
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
    schedule_report,
    withdraw_report,
)
from open_source_intelligence.reports.verification import (
    REASON_LOW_CITATION_COVERAGE,
    REASON_SENSITIVE_CONTENT,
    verify_report,
)

SUMMARY_KEY = "summary"
APPENDIX_KEY = "appendix"


def _sources(session: Session, report_id: int, section_key: str) -> list[ReportSource]:
    return list(
        session.scalars(
            select(ReportSource).where(
                ReportSource.report_id == report_id,
                ReportSource.section_key == section_key,
            )
        ).all()
    )


def _force_scheduled(
    session: Session,
    case: SeededReport,
    *,
    publish_at=None,
    verify: bool = True,
) -> Report:
    """Park a report in `scheduled` with (optionally) a passing verification."""
    if verify:
        verify_report(session, report_id=case.report_id, now=NOW)
    report = case.reload(session)
    report.status = "scheduled"
    report.publish_at = NOW if publish_at is None else publish_at
    session.commit()
    return report


def _prepare_case(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str,
    prior_publication: bool = True,
    repo_count: int = 2,
) -> SeededReport:
    """Build a report and, by default, give its topic an earlier published report."""
    case = build_report(session, workspace, publish_mode=publish_mode, repo_count=repo_count)
    if prior_publication:
        seed_published_sibling(session, case)
    return case


# --------------------------------------------------------------------------------------
# Policy evaluation
# --------------------------------------------------------------------------------------


def test_auto_with_delay_schedules_a_clean_report_after_the_window(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A routine internal report is queued, not blocked, once the topic is not a first run."""
    seed_published_sibling(session, report_case)

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_AUTO_WITH_DELAY
    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert decision.reason_codes == ()
    assert decision.publish_at == NOW + timedelta(minutes=60)


def test_delay_minutes_come_from_settings(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Operators control the withdrawal window through configuration."""
    seed_published_sibling(session, report_case)

    decision = evaluate_publish_policy(
        session,
        report_id=report_case.report_id,
        settings=build_settings(publish_delay_minutes=15),
        now=NOW,
    )

    assert decision.publish_at == NOW + timedelta(minutes=15)


def test_first_run_asks_for_review_on_manual(
    session: Session,
    workspace: Workspace,
) -> None:
    """A MANUAL report's first run has no baseline, so a human must sign off."""
    case = _prepare_case(session, workspace, publish_mode=MANUAL, prior_publication=False)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert decision.publish_at is None
    assert REASON_FIRST_RUN in decision.reason_codes


def test_auto_with_delay_skips_first_run_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """The unattended auto-publish path does not hold a topic's first report back.

    `AUTO_WITH_DELAY` already carries a verification gate and a withdrawal window, so requiring
    a human sign-off on the first report would stall every new auto-published topic. The first
    run must schedule instead of blocking, and must not carry `FIRST_RUN`.
    """
    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert REASON_FIRST_RUN not in decision.reason_codes
    assert decision.publish_at == NOW + timedelta(minutes=60)


def test_low_citation_coverage_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A report whose citations were pruned cannot auto-publish even on a known topic."""
    seed_published_sibling(session, report_case)
    for source in _sources(session, report_case.report_id, APPENDIX_KEY):
        session.delete(source)
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert REASON_VERIFICATION_FAILED in decision.reason_codes
    assert REASON_LOW_CITATION_COVERAGE in decision.reason_codes
    assert REASON_FIRST_RUN not in decision.reason_codes


def test_sensitive_artifact_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Credential-shaped text survives into the policy as a concrete block reason."""
    seed_published_sibling(session, report_case)
    report = report_case.reload(session)
    report.content_markdown += '\napi_key = "sk-abcdefghijklmnopqrst"\n'
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert REASON_SENSITIVE_CONTENT in decision.reason_codes


def test_confidential_sensitivity_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A confidential report is never released unattended."""
    seed_published_sibling(session, report_case)
    report = report_case.reload(session)
    report.sensitivity = "CONFIDENTIAL"
    report.visibility = "PRIVATE"
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert REASON_SENSITIVE_REPORT in decision.reason_codes


def test_manual_mode_always_requires_review(
    session: Session,
    workspace: Workspace,
) -> None:
    """`MANUAL` is a ceiling, so even a spotless report still waits for a person."""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_MANUAL
    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert decision.reason_codes == ()


def test_auto_requires_internal_visibility(
    session: Session,
    workspace: Workspace,
) -> None:
    """`AUTO` is reserved for internal reports; a narrower scope goes through review."""
    case = _prepare_case(session, workspace, publish_mode=AUTO)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)
    assert decision.requires_review is True
    assert REASON_NOT_INTERNAL_VISIBILITY in decision.reason_codes

    report = case.reload(session)
    report.visibility = "INTERNAL"
    session.commit()

    relaxed = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)
    assert relaxed.requires_review is False
    assert relaxed.publish_at == NOW


def test_auto_publishes_immediately_when_clean_and_internal(
    session: Session,
    workspace: Workspace,
) -> None:
    """`AUTO` has no withdrawal window: `publish_at` equals the decision moment."""
    case = _prepare_case(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    report.visibility = "INTERNAL"
    session.commit()

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_AUTO
    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert decision.publish_at == NOW


def test_evaluating_an_unknown_report_is_a_caller_error(session: Session) -> None:
    """Policy evaluation never silently succeeds on a missing report."""
    with pytest.raises(ReportGenerationError, match="does not exist"):
        evaluate_publish_policy(session, report_id=987654, now=NOW)


# --------------------------------------------------------------------------------------
# Human review
# --------------------------------------------------------------------------------------


def _park_for_review(session: Session, case: SeededReport) -> Report:
    report = case.reload(session)
    assert report.status == "draft"
    report.status = "pending_review"
    session.commit()
    return report


def test_record_review_approve_clears_for_publishing(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`approve` is explicit consent: the report becomes eligible for the queue."""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_APPROVE,
        reviewer_id=1,
        comment="looks good",
        now=NOW,
    )

    assert outcome.decision == REVIEW_APPROVE
    assert outcome.status == "approved"
    assert report_case.reload(session).status == "approved"
    row = session.scalar(select(ReportReview))
    assert row is not None
    assert row.decision == REVIEW_APPROVE
    assert row.version == 1


def test_record_review_request_changes_sends_it_back(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`request_changes` returns the report to the rewrite path, not the queue."""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_REQUEST_CHANGES,
        reviewer_id=1,
        now=NOW,
    )

    assert outcome.status == "changes_requested"
    with pytest.raises(ReportStateError):
        schedule_report(session, report_id=report_case.report_id, now=NOW)


def test_record_review_reject_terminates_as_failed(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`reject` is terminal, so the report can never be published afterwards."""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_REJECT,
        now=NOW,
    )

    assert outcome.status == "failed"
    with pytest.raises(ReportStateError):
        schedule_report(session, report_id=report_case.report_id, now=NOW)


def test_record_review_requires_pending_state(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A decision may only be recorded while the report actually awaits review."""
    report = report_case.reload(session)
    report.status = "scheduled"
    session.commit()

    with pytest.raises(ReportStateError, match="pending review"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            now=NOW,
        )


def test_record_review_rejects_an_unknown_decision(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Only the three declared decisions are accepted."""
    _park_for_review(session, report_case)

    with pytest.raises(ReportGenerationError, match="unsupported review decision"):
        record_review(session, report_id=report_case.report_id, decision="rubber_stamp", now=NOW)


def test_record_review_rejects_a_foreign_edited_version(
    session: Session,
    workspace: Workspace,
    report_case: SeededReport,
) -> None:
    """`edited_version_id` must name a version of *this* report.

    A foreign key only proves the row exists, so a version belonging to another report used
    to be recorded silently. The check also runs before the transition, so a refused review
    leaves the report where it was.
    """
    _park_for_review(session, report_case)
    other = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    foreign_version_id = session.scalar(
        select(ReportVersion.id).where(ReportVersion.report_id == other.report_id)
    )
    assert foreign_version_id is not None

    with pytest.raises(ReportGenerationError, match="no version row"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            edited_version_id=foreign_version_id,
            now=NOW,
        )

    assert report_case.reload(session).status == "pending_review"


def test_record_review_rejects_an_unknown_edited_version(
    session: Session,
    report_case: SeededReport,
) -> None:
    """An unknown id is a rejected request, not a database-level failure."""
    _park_for_review(session, report_case)

    with pytest.raises(ReportGenerationError, match="no version row"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            edited_version_id=999_999,
            now=NOW,
        )


# --------------------------------------------------------------------------------------
# Release preconditions (`INV-5`)
# --------------------------------------------------------------------------------------


def test_publish_report_requires_a_passing_verification(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Publishing without a verdict is exactly what `INV-5` forbids."""
    _force_scheduled(session, report_case, verify=False)

    with pytest.raises(PublishPolicyBlocked, match="no passing verification"):
        publish_report(session, report_id=report_case.report_id, now=NOW)

    assert report_case.reload(session).status == "scheduled"


def test_publish_report_requires_a_failed_verification_to_block(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A recorded *failed* verdict is just as blocking as a missing one."""
    report = report_case.reload(session)
    report.content_markdown = report.content_markdown.replace("## 本周摘要", "## 摘要", 1)
    session.commit()
    _force_scheduled(session, report_case, verify=True)

    with pytest.raises(PublishPolicyBlocked, match="no passing verification"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_manual_publish_requires_an_approval(
    session: Session,
    workspace: Workspace,
) -> None:
    """`MANUAL` reports carry an extra gate: the version must have been approved."""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)
    _force_scheduled(session, case, verify=True)

    with pytest.raises(PublishPolicyBlocked, match="no approve review"):
        publish_report(session, report_id=case.report_id, now=NOW)

    session.add(
        ReportReview(
            report_id=case.report_id,
            version=1,
            reviewer_id=1,
            decision=REVIEW_APPROVE,
            created_at=NOW,
        )
    )
    session.commit()

    published = publish_report(session, report_id=case.report_id, now=NOW)
    assert published.status == "published"


def test_publish_report_refuses_before_the_window_elapses(
    session: Session,
    report_case: SeededReport,
) -> None:
    """The withdrawal window is enforced inside the release, not by the caller."""
    _force_scheduled(session, report_case, publish_at=NOW + timedelta(hours=1))

    with pytest.raises(ReportStateError, match="withdrawal window has not elapsed"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_publish_report_requires_scheduled_state(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Only a queued report can be released."""
    with pytest.raises(ReportStateError, match="only a scheduled report"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_publish_report_records_a_run_and_audit(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A successful release leaves a `publish` pipeline run and an audit event."""
    _force_scheduled(session, report_case)

    published = publish_report(session, report_id=report_case.report_id, now=NOW)

    assert published.status == "published"
    assert published.published_at == NOW
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs
    assert runs[0].status == "succeeded"
    assert runs[0].metrics_json is not None
    assert runs[0].metrics_json["stage"] == "publish"
    # The pinned clock must close the run too, or `finished_at >= started_at` breaks.
    assert runs[0].started_at == NOW
    assert runs[0].finished_at == NOW
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "report.publish")).all()
    assert any(event.result == "success" for event in audits)


def test_withdraw_report_returns_a_published_report_to_draft(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Withdrawal is the only edge out of `published`, and it clears the publish window.

    Until this existed, a published report could not be corrected at all: editing needs a draft
    and rolling back needs a second version, so a single-version published report was stuck.
    """
    _force_scheduled(session, report_case)
    publish_report(session, report_id=report_case.report_id, now=NOW)
    published_version = session.get(Report, report_case.report_id).current_version

    withdrawn = withdraw_report(
        session,
        report_id=report_case.report_id,
        actor_id=1,
        comment="数字口径要改",
        now=NOW,
    )

    assert withdrawn.status == "draft"
    assert withdrawn.publish_at is None
    assert withdrawn.published_at is None
    # Withdrawing is not a delete and not a revision: the published version stays as it was.
    assert withdrawn.current_version == published_version
    events = session.scalars(select(AuditLog).where(AuditLog.action == "report.withdraw")).all()
    assert [event.result for event in events] == ["success"]
    assert events[0].metadata_json["version"] == published_version
    assert events[0].metadata_json["comment"] == "数字口径要改"


def test_withdraw_report_refuses_a_report_that_was_never_published(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A draft has nothing to withdraw; the state machine refuses the edge, not the caller."""
    assert report_case.reload(session).status == "draft"

    with pytest.raises(ReportStateError, match="only a published report"):
        withdraw_report(session, report_id=report_case.report_id, now=NOW)

    assert session.get(Report, report_case.report_id).status == "draft"


# --------------------------------------------------------------------------------------
# Applying the policy
# --------------------------------------------------------------------------------------


def test_publish_or_request_review_queues_a_clean_delayed_report(
    session: Session,
    report_case: SeededReport,
) -> None:
    """The end-to-end path verifies, decides, persists the verdict, and queues the report."""
    seed_published_sibling(session, report_case)

    outcome = publish_or_request_review(session, report_id=report_case.report_id, now=NOW)

    assert outcome.status == "scheduled"
    assert outcome.requires_review is False
    assert outcome.publish_at == NOW + timedelta(minutes=60)
    assert outcome.verification.passed is True
    assert outcome.pipeline_run_id is not None
    assert report_case.reload(session).publish_at == NOW + timedelta(minutes=60)
    assert int(session.scalar(select(func.count()).select_from(ReportVerification)) or 0) == 1
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs
    assert runs[0].started_at == NOW
    assert runs[0].finished_at == NOW


def test_publish_or_request_review_parks_a_manual_report(
    session: Session,
    workspace: Workspace,
) -> None:
    """Asking for a human is a normal result, so the decision run still succeeds."""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)

    outcome = publish_or_request_review(session, report_id=case.report_id, now=NOW)

    assert outcome.status == "pending_review"
    assert outcome.requires_review is True
    assert outcome.publish_at is None
    assert case.reload(session).status == "pending_review"
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs[0].status == "succeeded"
    assert runs[0].metrics_json is not None
    assert runs[0].metrics_json["resolution"] == "pending_review"


def test_publish_or_request_review_does_not_move_a_parked_report(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Re-evaluating a report already awaiting review must not advance it."""
    report = report_case.reload(session)
    report.status = "pending_review"
    session.commit()

    outcome = publish_or_request_review(session, report_id=report_case.report_id, now=NOW)

    assert outcome.status == "pending_review"
    assert report_case.reload(session).status == "pending_review"


def test_publish_or_request_review_refuses_terminal_states(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A failed or returned report must be regenerated before it can be published again."""
    report = report_case.reload(session)
    report.status = "failed"
    session.commit()

    with pytest.raises(ReportStateError, match="regenerate it"):
        publish_or_request_review(session, report_id=report_case.report_id, now=NOW)


# --------------------------------------------------------------------------------------
# The publishing sweep
# --------------------------------------------------------------------------------------


def test_publish_due_reports_leaves_early_reports_alone(
    session: Session,
    report_case: SeededReport,
) -> None:
    """A report still inside its window is not released by the sweep."""
    _force_scheduled(session, report_case, publish_at=NOW + timedelta(minutes=30))

    released = publish_due_reports(session, now=NOW)

    assert released == []
    assert report_case.reload(session).status == "scheduled"


def test_publish_due_reports_releases_due_reports(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Once the window has elapsed the sweep publishes the report."""
    _force_scheduled(session, report_case, publish_at=NOW - timedelta(minutes=1))

    released = publish_due_reports(session, now=NOW)

    assert len(released) == 1
    assert released[0]["report_id"] == report_case.report_id
    assert released[0]["status"] == "published"
    assert released[0]["error_code"] is None
    assert report_case.reload(session).status == "published"


def test_publish_due_reports_promotes_approved_reports(
    session: Session,
    report_case: SeededReport,
) -> None:
    """Explicit approval bypasses the withdrawal window inside a single sweep."""
    verify_report(session, report_id=report_case.report_id, now=NOW)
    report = report_case.reload(session)
    report.status = "approved"
    session.commit()

    released = publish_due_reports(session, now=NOW)

    assert len(released) == 1
    assert released[0]["status"] == "published"
    published = report_case.reload(session)
    assert published.status == "published"
    assert published.published_at == NOW


def test_publish_due_reports_marks_blocked_reports_and_continues(
    session: Session,
    workspace: Workspace,
) -> None:
    """One unobtainable verdict must not abort the release of the other reports."""
    good = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    blocked = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    _force_scheduled(session, good, verify=True)
    _force_scheduled(session, blocked, verify=False)

    released = publish_due_reports(session, now=NOW)

    by_id = {entry["report_id"]: entry for entry in released}
    assert by_id[good.report_id]["status"] == "published"
    assert by_id[blocked.report_id]["status"] == "failed"
    assert by_id[blocked.report_id]["error_code"] == PublishPolicyBlocked.code
    assert blocked.reload(session).status == "failed"
    assert blocked.reload(session).publish_at is None
    failed_runs = [
        run for run in list_pipeline_runs(session, run_type="publish") if run.status == "failed"
    ]
    assert failed_runs
    assert failed_runs[0].error_code == PublishPolicyBlocked.code
    assert failed_runs[0].started_at == NOW
    assert failed_runs[0].finished_at == NOW


def test_publish_due_reports_rejects_a_non_positive_limit(
    session: Session,
) -> None:
    """A sweep over nothing is a programming error, not a silent no-op."""
    with pytest.raises(ValueError, match="limit must be at least 1"):
        publish_due_reports(session, now=NOW, limit=0)
