"""Risk-based publishing: verify, decide, and drive the report state machine.

`ADR-003` splits publishing three ways: `AUTO` publishes a clean, low-sensitivity report
immediately, `AUTO_WITH_DELAY` parks it for a withdrawal window, and `MANUAL` waits for a
human. The point of the split is that a routine weekly report is never blocked on a person
while genuinely abnormal reports always are, so the policy below only escalates to
`MANUAL` when it can name a concrete reason.

The state machine is the one declared in `INTERFACES.md` §8:

```text
draft -> pending_review -> approved -> scheduled -> published
```

with `failed` as the terminal exception branch and `changes_requested` as the return branch.
`publish_due_reports` is the single driver that promotes `approved` reports and releases
`scheduled` ones once their withdrawal window has elapsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportVerification,
)
from open_source_intelligence.pipeline.runs import (
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.security.audit import record_audit_event

from .errors import (
    PublishPolicyBlocked,
    ReportGenerationError,
    ReportPublishError,
    ReportStateError,
)
from .verification import (
    REASON_LOW_CITATION_COVERAGE,
    REASON_SENSITIVE_CONTENT,
    VERDICT_PASSED,
    VerificationOutcome,
    verify_report,
)

PUBLISH_MODE_AUTO: Final[str] = "AUTO"
PUBLISH_MODE_AUTO_WITH_DELAY: Final[str] = "AUTO_WITH_DELAY"
PUBLISH_MODE_MANUAL: Final[str] = "MANUAL"

REASON_VERIFICATION_FAILED: Final[str] = "VERIFICATION_FAILED"
REASON_SENSITIVE_REPORT: Final[str] = "SENSITIVE_REPORT"
REASON_AGENT_RUN_NOT_CLEAN: Final[str] = "AGENT_RUN_NOT_CLEAN"
REASON_SECURITY_RISK: Final[str] = "SECURITY_RISK"
REASON_FIRST_RUN: Final[str] = "FIRST_RUN"
REASON_MODEL_OR_PROMPT_CHANGED: Final[str] = "MODEL_OR_PROMPT_CHANGED"
REASON_NOT_INTERNAL_VISIBILITY: Final[str] = "NOT_INTERNAL_VISIBILITY"

AGENT_PROMPT_INJECTION: Final[str] = "AGENT_PROMPT_INJECTION"
SENSITIVE_SENSITIVITY: Final[str] = "CONFIDENTIAL"
INTERNAL_VISIBILITY: Final[str] = "INTERNAL"
AGENT_RUN_COMPLETED: Final[str] = "completed"

REVIEW_APPROVE: Final[str] = "approve"
REVIEW_REJECT: Final[str] = "reject"
REVIEW_REQUEST_CHANGES: Final[str] = "request_changes"

#: Legal report-status transitions. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"pending_review", "scheduled", "failed"}),
    "pending_review": frozenset({"approved", "changes_requested", "failed"}),
    "approved": frozenset({"scheduled", "failed"}),
    "scheduled": frozenset({"published", "failed"}),
    "changes_requested": frozenset({"draft"}),
    "published": frozenset(),
    "failed": frozenset(),
}

#: Upper bound on reports released by one `publish_due_reports` pass.
PUBLISH_BATCH_LIMIT: Final[int] = 100


@dataclass(frozen=True, slots=True)
class PublishDecision:
    """The policy verdict for one report, shaped like the API `publish_decision` object."""

    report_id: int
    mode: str
    resolution: str
    reason_codes: tuple[str, ...]
    requires_review: bool
    publish_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "report_id": self.report_id,
            "mode": self.mode,
            "resolution": self.resolution,
            "reason_codes": list(self.reason_codes),
            "requires_review": self.requires_review,
            "publish_at": self.publish_at.isoformat() if self.publish_at else None,
        }


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """Result of applying the publish policy to one report."""

    report_id: int
    status: str
    mode: str
    reason_codes: tuple[str, ...]
    requires_review: bool
    publish_at: datetime | None
    pipeline_run_id: int | None
    verification: VerificationOutcome

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "report_id": self.report_id,
            "status": self.status,
            "mode": self.mode,
            "reason_codes": list(self.reason_codes),
            "requires_review": self.requires_review,
            "publish_at": self.publish_at.isoformat() if self.publish_at else None,
            "pipeline_run_id": self.pipeline_run_id,
            "verification": self.verification.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """Result of recording one human review decision."""

    report_id: int
    review_id: int
    decision: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "report_id": self.report_id,
            "review_id": self.review_id,
            "decision": self.decision,
            "status": self.status,
        }


def _transition(report: Report, target: str) -> None:
    """Move a report to `target`, rejecting any edge the state machine forbids."""
    allowed = ALLOWED_TRANSITIONS.get(report.status, frozenset())
    if target not in allowed:
        raise ReportStateError(f"report {report.id} cannot move from {report.status} to {target}")
    report.status = target


def _run_closed_at(now: datetime | None, moment: datetime) -> datetime | None:
    """Echo a pinned clock into the run's close time, or `None` to use the wall clock.

    Each publishing run is started at the caller's `moment`, and `pipeline_runs` enforces
    `finished_at >= started_at`. Closing it from the wall clock while `moment` was pinned
    would make the record inconsistent, so a pinned `now` is carried through instead.
    """
    return moment if now is not None else None


def _config_fingerprint(session: Session, agent_run: AgentRun) -> tuple[str, str, str]:
    """Return the model, prompt version, and tool policy a run was executed with."""
    config = session.get(AgentConfig, agent_run.agent_config_id)
    if config is None:
        return ("", "", "")
    return (
        config.model,
        config.prompt_version,
        json.dumps(config.tool_policy_json, sort_keys=True),
    )


def _latest_published_report(session: Session, *, topic_id: int, exclude_id: int) -> Report | None:
    """Return the most recently published sibling report, if the topic has one."""
    return session.scalars(
        select(Report)
        .where(
            Report.topic_id == topic_id,
            Report.status == "published",
            Report.id != exclude_id,
        )
        .order_by(Report.published_at.desc(), Report.id.desc())
        .limit(1)
    ).first()


def _baseline_reasons(session: Session, report: Report) -> list[str]:
    """Collect reasons that come from the run and the topic history, not the artifact."""
    reasons: list[str] = []
    agent_run = session.get(AgentRun, report.agent_run_id)
    if agent_run is None:
        return [REASON_AGENT_RUN_NOT_CLEAN]
    if agent_run.error_code == AGENT_PROMPT_INJECTION:
        reasons.append(REASON_SECURITY_RISK)
    if agent_run.status != AGENT_RUN_COMPLETED or agent_run.error_code is not None:
        reasons.append(REASON_AGENT_RUN_NOT_CLEAN)

    previous = _latest_published_report(session, topic_id=report.topic_id, exclude_id=report.id)
    if previous is None:
        reasons.append(REASON_FIRST_RUN)
        return reasons
    previous_run = session.get(AgentRun, previous.agent_run_id)
    if previous_run is None:
        return reasons
    if _config_fingerprint(session, agent_run) != _config_fingerprint(session, previous_run):
        reasons.append(REASON_MODEL_OR_PROMPT_CHANGED)
    return reasons


def evaluate_publish_policy(
    session: Session,
    *,
    report_id: int,
    verification: VerificationOutcome | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PublishDecision:
    """Decide whether a report publishes immediately, after a delay, or after review.

    The configured `publish_mode` sets the ceiling; concrete findings can only make the
    decision stricter, never looser.
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")
    outcome = verification or verify_report(
        session,
        report_id=report_id,
        settings=resolved_settings,
        now=moment,
    )

    reasons: list[str] = []
    if not outcome.passed:
        reasons.append(REASON_VERIFICATION_FAILED)
    if REASON_LOW_CITATION_COVERAGE in outcome.reason_codes:
        reasons.append(REASON_LOW_CITATION_COVERAGE)
    if REASON_SENSITIVE_CONTENT in outcome.reason_codes:
        reasons.append(REASON_SENSITIVE_CONTENT)
    if report.sensitivity == SENSITIVE_SENSITIVITY:
        reasons.append(REASON_SENSITIVE_REPORT)
    reasons.extend(_baseline_reasons(session, report))
    if report.publish_mode == PUBLISH_MODE_AUTO and report.visibility != INTERNAL_VISIBILITY:
        # `AUTO` is reserved for internal reports; anything narrower goes through review.
        reasons.append(REASON_NOT_INTERNAL_VISIBILITY)

    unique_reasons = tuple(dict.fromkeys(reasons))
    requires_review = report.publish_mode == PUBLISH_MODE_MANUAL or bool(unique_reasons)
    if requires_review:
        return PublishDecision(
            report_id=report.id,
            mode=report.publish_mode,
            resolution="pending_review",
            reason_codes=unique_reasons,
            requires_review=True,
            publish_at=None,
        )
    delay = (
        timedelta(minutes=resolved_settings.publish_delay_minutes)
        if report.publish_mode == PUBLISH_MODE_AUTO_WITH_DELAY
        else timedelta(0)
    )
    publish_at = moment + delay
    return PublishDecision(
        report_id=report.id,
        mode=report.publish_mode,
        resolution="scheduled",
        reason_codes=(),
        requires_review=False,
        publish_at=publish_at,
    )


def schedule_report(session: Session, *, report_id: int, now: datetime | None = None) -> Report:
    """Move an approved report onto the publishing queue."""
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")
    _transition(report, "scheduled")
    report.publish_at = moment
    session.commit()
    return report


def publish_report(
    session: Session,
    *,
    report_id: int,
    now: datetime | None = None,
) -> Report:
    """Publish a scheduled report, asserting the `INV-5` preconditions first.

    The delay window is enforced here rather than in the caller so no code path can release
    a report early. A `MANUAL` report must additionally carry an `approve` review for the
    version being published.
    """
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")
    if report.status != "scheduled":
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a scheduled report can be published"
        )
    if report.publish_at is not None and report.publish_at > moment:
        raise ReportStateError(
            f"report {report.id} withdrawal window has not elapsed until "
            f"{report.publish_at.isoformat()}"
        )
    verification = session.scalar(
        select(ReportVerification).where(
            ReportVerification.report_id == report.id,
            ReportVerification.version == report.current_version,
        )
    )
    if verification is None or verification.verdict != VERDICT_PASSED:
        raise PublishPolicyBlocked(
            f"report {report.id} version {report.current_version} has no passing verification"
        )
    if report.publish_mode == PUBLISH_MODE_MANUAL:
        approval = session.scalar(
            select(ReportReview).where(
                ReportReview.report_id == report.id,
                ReportReview.version == report.current_version,
                ReportReview.decision == REVIEW_APPROVE,
            )
        )
        if approval is None:
            raise PublishPolicyBlocked(f"report {report.id} is MANUAL and has no approve review")

    pipeline_run = start_pipeline_run(
        session,
        run_type="publish",
        topic_id=report.topic_id,
        attempt=1,
    )
    mark_pipeline_run_running(session, pipeline_run, started_at=moment)
    _transition(report, "published")
    report.published_at = moment
    record_audit_event(
        session,
        action="report.publish",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        metadata_json={
            "version": report.current_version,
            "publish_mode": report.publish_mode,
        },
    )
    finish_pipeline_run(
        session,
        pipeline_run,
        status="succeeded",
        metrics_json={
            "report_id": report.id,
            "version": report.current_version,
            "publish_mode": report.publish_mode,
            "stage": "publish",
        },
        finished_at=_run_closed_at(now, moment),
    )
    return report


def publish_or_request_review(
    session: Session,
    *,
    report_id: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PublishOutcome:
    """Verify a report, apply the publish policy, and record the decision.

    The `publish` run written here tracks the *decision*, not the eventual release: it
    succeeds whenever the policy was applied cleanly, even when the outcome is
    `pending_review`, because asking for a human is a normal result rather than an error.
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")
    if report.status in {"changes_requested", "failed"}:
        raise ReportStateError(
            f"report {report.id} is {report.status}; regenerate it before publishing"
        )

    pipeline_run = start_pipeline_run(
        session,
        run_type="publish",
        topic_id=report.topic_id,
        attempt=1,
    )
    mark_pipeline_run_running(session, pipeline_run, started_at=moment)
    verification = verify_report(
        session,
        report_id=report_id,
        settings=resolved_settings,
        now=moment,
    )
    decision = evaluate_publish_policy(
        session,
        report_id=report_id,
        verification=verification,
        settings=resolved_settings,
        now=moment,
    )

    if report.status in {"pending_review", "approved", "scheduled", "published"}:
        # Already parked downstream of the decision: re-evaluating must not silently move
        # it, because `pending_review` may only be cleared by a human reviewer.
        pass
    elif decision.requires_review:
        _transition(report, "pending_review")
        report.publish_at = None
    else:
        _transition(report, "scheduled")
        report.publish_at = decision.publish_at
    session.flush()

    finish_pipeline_run(
        session,
        pipeline_run,
        status="succeeded",
        metrics_json={
            "report_id": report.id,
            "version": report.current_version,
            "publish_mode": report.publish_mode,
            "stage": "evaluate",
            "resolution": decision.resolution,
            "reason_codes": list(decision.reason_codes),
            "requires_review": decision.requires_review,
            "publish_at": decision.publish_at.isoformat() if decision.publish_at else None,
        },
        finished_at=_run_closed_at(now, moment),
    )
    return PublishOutcome(
        report_id=report.id,
        status=report.status,
        mode=report.publish_mode,
        reason_codes=decision.reason_codes,
        requires_review=decision.requires_review,
        publish_at=report.publish_at,
        pipeline_run_id=pipeline_run.id,
        verification=verification,
    )


def record_review(
    session: Session,
    *,
    report_id: int,
    decision: str,
    reviewer_id: int | None = None,
    comment: str | None = None,
    edited_version_id: int | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """Record one human decision on a report awaiting review.

    `approve` clears the report for publishing, `request_changes` sends it back for a
    rewrite, and `reject` terminates it as `failed` so it can never be published.
    """
    moment = now or utc_now()
    if decision not in {REVIEW_APPROVE, REVIEW_REJECT, REVIEW_REQUEST_CHANGES}:
        raise ReportGenerationError(f"unsupported review decision: {decision}")
    report = session.get(Report, report_id)
    if report is None:
        raise ReportGenerationError(f"report {report_id} does not exist")
    if report.status != "pending_review":
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a report pending review can be reviewed"
        )
    target = {
        REVIEW_APPROVE: "approved",
        REVIEW_REJECT: "failed",
        REVIEW_REQUEST_CHANGES: "changes_requested",
    }[decision]
    _transition(report, target)
    review = ReportReview(
        report_id=report.id,
        version=report.current_version,
        reviewer_id=reviewer_id,
        decision=decision,
        comment=comment,
        edited_version_id=edited_version_id,
        created_at=moment,
    )
    session.add(review)
    record_audit_event(
        session,
        action="report.review",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        actor_id=reviewer_id,
        metadata_json={
            "version": report.current_version,
            "decision": decision,
            "status": target,
        },
    )
    session.commit()
    return ReviewOutcome(
        report_id=report.id,
        review_id=review.id,
        decision=decision,
        status=target,
    )


def _fail_publish(
    session: Session,
    report: Report,
    error: ReportPublishError,
    moment: datetime,
    *,
    finished_at: datetime | None = None,
) -> None:
    """Record a blocked release and stop the report from being retried on every sweep.

    Leaving a policy-blocked report in `scheduled` would make it retry forever and stay
    invisible; `failed` is terminal, so it surfaces as work that needs a human.
    """
    pipeline_run = start_pipeline_run(
        session,
        run_type="publish",
        topic_id=report.topic_id,
        attempt=1,
    )
    mark_pipeline_run_running(session, pipeline_run, started_at=moment)
    report.status = "failed"
    report.publish_at = None
    record_audit_event(
        session,
        action="report.publish",
        resource_type="report",
        resource_id=str(report.id),
        result="failed",
        metadata_json={"error_code": error.code, "version": report.current_version},
    )
    fail_pipeline_run(
        session,
        pipeline_run,
        error_code=error.code,
        error_message=str(error),
        metrics_json={
            "stage": "publish",
            "report_id": report.id,
            "version": report.current_version,
        },
        finished_at=finished_at,
    )


def publish_due_reports(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = PUBLISH_BATCH_LIMIT,
) -> list[dict[str, Any]]:
    """Release everything that is due, promoting human-approved reports on the way.

    An approval is explicit consent, so an `approved` report skips the withdrawal window
    and is queued for immediate release; `scheduled` reports are released only once
    `publish_at` has elapsed. One blocked report never stops the rest of the sweep: it is
    marked `failed` with its stable error code and the sweep continues, mirroring how a
    single failing topic cannot abort the daily pipeline.
    """
    moment = now or utc_now()
    if limit < 1:
        raise ValueError("limit must be at least 1")
    approved = session.scalars(
        select(Report).where(Report.status == "approved").order_by(Report.id).limit(limit)
    ).all()
    for report in approved:
        schedule_report(session, report_id=report.id, now=moment)

    due = session.scalars(
        select(Report)
        .where(Report.status == "scheduled", Report.publish_at <= moment)
        .order_by(Report.publish_at, Report.id)
        .limit(limit)
    ).all()
    released: list[dict[str, Any]] = []
    for report in due:
        try:
            published = publish_report(session, report_id=report.id, now=moment)
        except ReportPublishError as exc:
            _fail_publish(
                session,
                report,
                exc,
                moment,
                finished_at=_run_closed_at(now, moment),
            )
            released.append(
                {
                    "report_id": report.id,
                    "status": "failed",
                    "publish_mode": report.publish_mode,
                    "error_code": exc.code,
                    "error_message": str(exc)[:256],
                    "published_at": None,
                }
            )
            continue
        released.append(
            {
                "report_id": published.id,
                "status": published.status,
                "publish_mode": published.publish_mode,
                "error_code": None,
                "error_message": None,
                "published_at": published.published_at.isoformat()
                if published.published_at
                else None,
            }
        )
    return released
