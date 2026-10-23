"""Report push service: guard, allowlist, deliver, record, retry.

The service is the only place a report leaves the system, so the ordering below is the whole
security story:

1. **Guard** the report's status. Only an `approved` or `published` report may be pushed, which
   is what "only approved reports may be pushed" means in practice — a merely generated draft
   has no business being mailed anywhere.
2. **Guard** the audience. A `CONFIDENTIAL` report additionally needs a human `approve` review
   for the version being sent, because `REQUIREMENTS.md` §7 reserves external delivery of
   sensitive material for `MANUAL`.
3. **Resolve** the destination against `push_targets`. The allowlist is the only way to reach a
   real address, and a target has to be enabled and explicitly cleared for the report's
   visibility and sensitivity.
4. **Deliver** through a channel adapter, then **record** the attempt.

Delivery failure is a recorded outcome, not an exception: the `report_pushes` row keeps the
error code and its attempt count, so `retry_failed_pushes` can pick it up and a repeated request
for the same (report, version, target) replays the stored result instead of sending twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.persistence.models.notifications import (
    PushTarget,
    ReportPush,
)
from open_source_intelligence.persistence.models.reporting import Report, ReportReview
from open_source_intelligence.pipeline.runs import (
    fail_pipeline_run,
    finish_pipeline_run,
    mark_pipeline_run_running,
    start_pipeline_run,
)
from open_source_intelligence.security.audit import record_audit_event

from .channels import DeliveryReceipt, NotificationChannel, OutboundMessage, build_channel
from .channels.webhook_channel import validate_webhook_url
from .errors import (
    PushApprovalRequired,
    PushBlocked,
    PushDeliveryFailed,
    PushDisabled,
    PushError,
    PushNotFound,
    PushTargetNotAllowed,
)

#: Run type recorded for a push attempt; registered alongside the other topic-scoped runs.
PUSH_RUN_TYPE: Final[str] = "push"
#: Upper bound on pushes a single retry sweep attempts.
PUSH_BATCH_LIMIT: Final[int] = 100
#: The only statuses a report may be pushed from.
PUSHABLE_STATUSES: Final[frozenset[str]] = frozenset({"approved", "published"})

CONFIDENTIAL_SENSITIVITY: Final[str] = "CONFIDENTIAL"
PRIVATE_VISIBILITY: Final[str] = "PRIVATE"
REVIEW_APPROVE: Final[str] = "approve"

#: Longest transport detail persisted on a push row.
MAX_DETAIL_LENGTH: Final[int] = 512
#: Error code recorded when a transport refuses or fails a delivery.
PUSH_ERROR_CODE: Final[str] = "PUSH_DELIVERY_FAILED"
#: Error code recorded when a retry targets a version the report has already left behind.
PUSH_VERSION_SUPERSEDED: Final[str] = "PUSH_VERSION_SUPERSEDED"
#: How much of the report body travels with a notification.
BODY_EXCERPT_CHARS: Final[int] = 800
SUBJECT_PREFIX: Final[str] = "[开源技术情报]"


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """Result of one delivery attempt, successful or not."""

    push_id: int
    report_id: int
    version: int
    channel: str
    destination: str
    status: str
    attempt: int
    error_code: str | None
    error_message: str | None
    delivered: bool
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "push_id": self.push_id,
            "report_id": self.report_id,
            "version": self.version,
            "channel": self.channel,
            "destination": self.destination,
            "status": self.status,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "delivered": self.delivered,
            "replayed": self.replayed,
        }


def _outcome(row: ReportPush, *, replayed: bool = False) -> PushOutcome:
    """Build one outcome from a persisted push row."""
    return PushOutcome(
        push_id=row.id,
        report_id=row.report_id,
        version=row.version,
        channel=row.channel,
        destination=row.destination,
        status=row.status,
        attempt=row.attempt,
        error_code=row.error_code,
        error_message=row.error_message,
        delivered=row.status == "sent",
        replayed=replayed,
    )


def _require_report(session: Session, report_id: int) -> Report:
    """Load a report or fail with a stable error."""
    report = session.get(Report, report_id)
    if report is None:
        raise PushNotFound(f"report {report_id} does not exist")
    return report


def _has_approval(session: Session, report: Report) -> bool:
    """Return whether a human approved the version currently being pushed."""
    approval = session.scalar(
        select(ReportReview.id).where(
            ReportReview.report_id == report.id,
            ReportReview.version == report.current_version,
            ReportReview.decision == REVIEW_APPROVE,
        )
    )
    return approval is not None


def build_message(session: Session, report: Report) -> OutboundMessage:
    """Render the notification a target receives for one report version."""
    topic = session.get(Topic, report.topic_id)
    lines = [
        f"报告：{report.title}",
        f"主题：{topic.name if topic is not None else report.topic_id}",
        f"版本：v{report.current_version}",
        f"状态：{report.status}",
        f"可见范围：{report.visibility}",
        f"敏感级别：{report.sensitivity}",
        f"发布时间：{report.published_at.isoformat() if report.published_at else '尚未发布'}",
        "",
        report.content_markdown.strip()[:BODY_EXCERPT_CHARS],
    ]
    return OutboundMessage(
        report_id=report.id,
        version=report.current_version,
        subject=f"{SUBJECT_PREFIX} {report.title}",
        body="\n".join(lines),
    )


def _resolve_target(
    session: Session,
    report: Report,
    *,
    channel: str,
    target_id: int,
) -> PushTarget:
    """Return the allowlisted target, refusing anything not cleared for this report."""
    target = session.get(PushTarget, target_id)
    if target is None:
        raise PushTargetNotAllowed(f"push target {target_id} is not on the allowlist")
    if not target.enabled:
        raise PushTargetNotAllowed(f"push target {target_id} is disabled")
    if target.channel != channel:
        raise PushTargetNotAllowed(
            f"push target {target_id} uses channel {target.channel}, not {channel}"
        )
    if target.channel == "webhook":
        validate_webhook_url(target.destination)
    if report.visibility == PRIVATE_VISIBILITY and not target.allow_private:
        raise PushTargetNotAllowed(f"push target {target_id} is not cleared for PRIVATE reports")
    if report.sensitivity == CONFIDENTIAL_SENSITIVITY and not target.allow_confidential:
        raise PushTargetNotAllowed(
            f"push target {target_id} is not cleared for CONFIDENTIAL reports"
        )
    return target


def list_targets(
    session: Session,
    *,
    channel: str | None = None,
    enabled_only: bool = True,
) -> list[PushTarget]:
    """Return the allowlist, newest first, optionally filtered."""
    statement = select(PushTarget)
    if channel is not None:
        statement = statement.where(PushTarget.channel == channel)
    if enabled_only:
        statement = statement.where(PushTarget.enabled.is_(True))
    return list(session.scalars(statement.order_by(PushTarget.id)).all())


def list_pushes(
    session: Session,
    *,
    report_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    access_filter: ColumnElement[bool] | None = None,
) -> list[ReportPush]:
    """Return delivery records, newest first, optionally filtered.

    `access_filter` is the report ACL as a SQL predicate. The ledger names report titles, versions,
    destinations and error text, so a role check alone is not enough: it has to be filtered in the
    query, exactly like the report list, or a caller can read the metadata of a report it may not
    open. `None` means "system scope" and is only correct for a background sweep.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = select(ReportPush)
    if access_filter is not None:
        statement = statement.join(Report, Report.id == ReportPush.report_id).where(access_filter)
    if report_id is not None:
        statement = statement.where(ReportPush.report_id == report_id)
    if status is not None:
        statement = statement.where(ReportPush.status == status)
    return list(session.scalars(statement.order_by(ReportPush.id.desc()).limit(limit)).all())


def push_report(
    session: Session,
    *,
    report_id: int,
    channel: str,
    target_id: int,
    actor_id: int | None = None,
    settings: Settings | None = None,
    notifier: NotificationChannel | None = None,
    now: datetime | None = None,
    version: int | None = None,
) -> PushOutcome:
    """Deliver one report version to one allowlisted target.

    Guard violations raise; a transport failure is recorded and returned with `status="failed"`.
    Re-requesting a delivery that already succeeded returns the stored result untouched, which
    is what makes the whole operation idempotent per (report, version, target).
    """
    resolved = settings or get_settings()
    if not resolved.push_enabled:
        raise PushDisabled("pushing is disabled by PUSH_ENABLED")
    moment = now or utc_now()
    report = _require_report(session, report_id)
    target_version = report.current_version if version is None else version
    if target_version != report.current_version:
        raise PushBlocked(
            f"push version {target_version} is not current; report {report.id} is at "
            f"version {report.current_version}"
        )
    if report.status not in PUSHABLE_STATUSES:
        allowed = " or ".join(sorted(PUSHABLE_STATUSES))
        raise PushBlocked(
            f"report {report.id} is {report.status}; only {allowed} reports can be pushed"
        )
    if report.sensitivity == CONFIDENTIAL_SENSITIVITY and not _has_approval(session, report):
        raise PushApprovalRequired(
            f"report {report.id} is CONFIDENTIAL and version {report.current_version} "
            "has no approve review"
        )
    target = _resolve_target(session, report, channel=channel, target_id=target_id)

    key = ReportPush.key_for(
        report_id=report.id,
        version=target_version,
        target_id=target.id,
    )
    existing = session.scalar(select(ReportPush).where(ReportPush.idempotency_key == key))
    if existing is not None and existing.status == "sent":
        return _outcome(existing, replayed=True)
    attempt = 1 if existing is None else existing.attempt + 1
    if existing is not None and attempt > resolved.push_max_attempts:
        raise PushDeliveryFailed(
            f"push {key} failed {existing.attempt} times; the retry budget of "
            f"{resolved.push_max_attempts} is exhausted"
        )

    # Built before anything is written: an unconfigured live channel is a configuration error,
    # not a delivery failure, and it must not leave a push row behind.
    adapter = notifier or build_channel(resolved, channel)
    message = build_message(session, report)

    row = existing or ReportPush(
        report_id=report.id,
        target_id=target.id,
        version=target_version,
        channel=target.channel,
        destination=target.destination,
        idempotency_key=key,
        status="pending",
        attempt=0,
        created_at=moment,
        updated_at=moment,
    )
    if existing is None:
        session.add(row)
    else:
        row.channel = target.channel
        row.destination = target.destination
    pipeline_run = start_pipeline_run(
        session,
        run_type=PUSH_RUN_TYPE,
        topic_id=report.topic_id,
        attempt=attempt,
    )
    mark_pipeline_run_running(session, pipeline_run, started_at=moment)
    row.pipeline_run_id = pipeline_run.id
    row.message_json = message.to_dict()
    row.attempt = attempt
    row.updated_at = moment
    session.flush()

    receipt = _deliver(adapter, destination=target.destination, message=message)
    row.status = "sent" if receipt.ok else "failed"
    row.response_excerpt = (
        None if receipt.detail is None else redact_text(receipt.detail)[:MAX_DETAIL_LENGTH]
    )
    if receipt.ok:
        row.sent_at = moment
        row.error_code = None
        row.error_message = None
    else:
        row.error_code = PUSH_ERROR_CODE
        row.error_message = redact_text(receipt.detail or "")[:MAX_DETAIL_LENGTH] or None
    row.updated_at = moment

    record_audit_event(
        session,
        action="report.push",
        resource_type="report",
        resource_id=str(report.id),
        result="success" if receipt.ok else "failed",
        actor_id=actor_id,
        metadata_json={
            "version": report.current_version,
            "channel": target.channel,
            "target_id": target.id,
            "attempt": attempt,
            "error_code": row.error_code,
            "replayed": False,
        },
    )
    metrics: dict[str, Any] = {
        "stage": "push",
        "report_id": report.id,
        "version": report.current_version,
        "channel": target.channel,
        "target_id": target.id,
    }
    if receipt.ok:
        finish_pipeline_run(
            session,
            pipeline_run,
            status="succeeded",
            metrics_json=metrics,
            finished_at=_closed_at(now, moment),
        )
    else:
        fail_pipeline_run(
            session,
            pipeline_run,
            error_code=PUSH_ERROR_CODE,
            error_message=row.error_message or "",
            metrics_json=metrics,
            finished_at=_closed_at(now, moment),
        )
    return _outcome(row)


def _deliver(
    adapter: NotificationChannel,
    *,
    destination: str,
    message: OutboundMessage,
) -> DeliveryReceipt:
    """Send through an adapter, converting any unexpected failure into a receipt.

    A push sits inside the publish path, so an adapter bug must never be able to turn a
    successful release into a crashed run; it becomes a recorded, retryable failure instead.
    """
    try:
        return adapter.send(destination=destination, message=message)
    except Exception as exc:  # A broken adapter must not be able to lose the push record.
        return DeliveryReceipt(
            ok=False,
            detail=f"adapter:{exc.__class__.__name__}:{exc}"[:MAX_DETAIL_LENGTH],
        )


def _closed_at(now: datetime | None, moment: datetime) -> datetime | None:
    """Echo a pinned clock into a run's close time, matching the publishing convention."""
    return moment if now is not None else None


def push_after_publish(
    session: Session,
    *,
    report_id: int,
    actor_id: int | None = None,
    settings: Settings | None = None,
    notifier: NotificationChannel | None = None,
    now: datetime | None = None,
) -> list[PushOutcome]:
    """Auto-push a freshly published report to every enabled target.

    This runs inside the publish path, so it never raises: an unusable target or a refused
    transport is recorded (or skipped) and the release stands. A push failure is a retry
    candidate, never a reason to un-publish a report.
    """
    resolved = settings or get_settings()
    if not resolved.push_enabled or not resolved.push_auto_after_publish:
        return []
    outcomes: list[PushOutcome] = []
    for target in list_targets(session):
        try:
            outcomes.append(
                push_report(
                    session,
                    report_id=report_id,
                    channel=target.channel,
                    target_id=target.id,
                    actor_id=actor_id,
                    settings=resolved,
                    notifier=notifier,
                    now=now,
                )
            )
        except Exception as exc:  # Auto-push must never break the release, whatever fails.
            record_audit_event(
                session,
                action="report.push",
                resource_type="report",
                resource_id=str(report_id),
                result="denied",
                actor_id=actor_id,
                metadata_json={
                    "channel": target.channel,
                    "target_id": target.id,
                    "auto": True,
                    "error": redact_text(str(exc))[:MAX_DETAIL_LENGTH],
                },
            )
            session.commit()
    return outcomes


def retry_push(
    session: Session,
    *,
    push_id: int,
    actor_id: int | None = None,
    settings: Settings | None = None,
    notifier: NotificationChannel | None = None,
    now: datetime | None = None,
) -> PushOutcome:
    """Retry one recorded push by re-running its (report, version, target) triple."""
    row = session.get(ReportPush, push_id)
    if row is None:
        raise PushNotFound(f"push {push_id} does not exist")
    return push_report(
        session,
        report_id=row.report_id,
        channel=row.channel,
        target_id=row.target_id,
        actor_id=actor_id,
        settings=settings,
        notifier=notifier,
        now=now,
        version=row.version,
    )


def retry_failed_pushes(
    session: Session,
    *,
    limit: int = PUSH_BATCH_LIMIT,
    actor_id: int | None = None,
    settings: Settings | None = None,
    notifier: NotificationChannel | None = None,
    now: datetime | None = None,
    access_filter: ColumnElement[bool] | None = None,
) -> list[PushOutcome]:
    """Retry every failed push that still has retry budget left.

    One unusable row never stops the sweep: mirroring `publish_due_reports`, a failed retry is
    skipped and the rest of the batch continues.

    `access_filter` narrows the sweep to the reports the caller may read. An operator pressing
    "retry" is acting on their own reports, not silently re-sending material they cannot open;
    the scheduled sweep leaves it `None` because the beat task belongs to no single user.
    """
    resolved = settings or get_settings()
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not resolved.push_enabled:
        return []
    statement = select(ReportPush).where(
        ReportPush.status == "failed",
        ReportPush.attempt < resolved.push_max_attempts,
    )
    if access_filter is not None:
        statement = statement.join(Report, Report.id == ReportPush.report_id).where(access_filter)
    candidates = session.scalars(
        statement.order_by(ReportPush.updated_at, ReportPush.id).limit(limit)
    ).all()
    outcomes: list[PushOutcome] = []
    for row in candidates:
        report = session.get(Report, row.report_id)
        if report is None or row.version != report.current_version:
            row.attempt = resolved.push_max_attempts
            row.error_code = PUSH_VERSION_SUPERSEDED
            row.error_message = (
                f"version {row.version} is no longer current"
                if report is not None
                else "report no longer exists"
            )
            row.updated_at = now or utc_now()
            record_audit_event(
                session,
                action="report.push",
                resource_type="report",
                resource_id=str(row.report_id),
                result="denied",
                actor_id=actor_id,
                metadata_json={
                    "channel": row.channel,
                    "target_id": row.target_id,
                    "retry": True,
                    "error_code": PUSH_VERSION_SUPERSEDED,
                },
            )
            session.commit()
            continue
        try:
            outcomes.append(
                push_report(
                    session,
                    report_id=row.report_id,
                    channel=row.channel,
                    target_id=row.target_id,
                    actor_id=actor_id,
                    settings=resolved,
                    notifier=notifier,
                    now=now,
                    version=row.version,
                )
            )
        except PushError as exc:
            # A row can stop being retryable (the report left the pushable statuses, or the
            # target was disabled). Record why instead of dropping it silently.
            record_audit_event(
                session,
                action="report.push",
                resource_type="report",
                resource_id=str(row.report_id),
                result="denied",
                actor_id=actor_id,
                metadata_json={
                    "channel": row.channel,
                    "target_id": row.target_id,
                    "retry": True,
                    "error_code": exc.code,
                },
            )
            session.commit()
        except Exception:  # One bad row must not abort the sweep.
            session.rollback()
    return outcomes
