"""Tests for the report push service: guards, allowlist, delivery, idempotency, and retries."""

from __future__ import annotations

import pytest
from notifications_support import APPROVED, PUBLISHED, StubChannel, seed_pushable_report
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_settings

from open_source_intelligence.notifications.channels import (
    OfflineChannel,
    OutboundMessage,
    build_channel,
    sign_body,
)
from open_source_intelligence.notifications.channels.webhook_channel import (
    validate_webhook_destination,
    validate_webhook_url,
)
from open_source_intelligence.notifications.errors import (
    PushApprovalRequired,
    PushBlocked,
    PushDeliveryFailed,
    PushDisabled,
    PushNotFound,
    PushTargetNotAllowed,
)
from open_source_intelligence.notifications.service import (
    PUSH_VERSION_SUPERSEDED,
    build_message,
    list_pushes,
    list_targets,
    push_after_publish,
    push_report,
    retry_failed_pushes,
    retry_push,
)
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Topic
from open_source_intelligence.persistence.models.notifications import ReportPush
from open_source_intelligence.persistence.models.reporting import Report, ReportReview

NON_PUSHABLE_STATUSES = (
    "draft",
    "pending_review",
    "scheduled",
    "failed",
    "changes_requested",
)


def _pushes(session: Session, report_id: int) -> list[ReportPush]:
    """Return the delivery rows for one report."""
    return list(
        session.scalars(
            select(ReportPush).where(ReportPush.report_id == report_id).order_by(ReportPush.id)
        ).all()
    )


def _approve(session: Session, report: Report) -> None:
    """Record a human approval for the report's current version."""
    session.add(
        ReportReview(
            report_id=report.id,
            version=report.current_version,
            reviewer_id=1,
            decision="approve",
        )
    )
    session.commit()


@pytest.mark.parametrize("status", NON_PUSHABLE_STATUSES)
def test_only_pushable_statuses_may_be_pushed(
    session: Session,
    workspace: Workspace,
    status: str,
) -> None:
    """A report that has not cleared review or been released cannot leave the system."""
    report = seed_pushable_report(session, workspace, status=status)
    target = workspace.add_push_target()

    with pytest.raises(PushBlocked) as failure:
        push_report(
            session,
            report_id=report.id,
            channel="email",
            target_id=target.id,
            notifier=StubChannel(),
        )

    assert failure.value.code == "PUSH_BLOCKED"
    assert status in str(failure.value)
    assert _pushes(session, report.id) == []


@pytest.mark.parametrize("status", [APPROVED, PUBLISHED])
def test_approved_and_published_reports_may_be_pushed(
    session: Session,
    workspace: Workspace,
    status: str,
) -> None:
    """Both sides of the rule are allowed: cleared for release, or already released."""
    report = seed_pushable_report(session, workspace, status=status)
    target = workspace.add_push_target()
    notifier = StubChannel()

    outcome = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        notifier=notifier,
    )

    assert outcome.delivered is True
    assert outcome.status == "sent"
    assert outcome.version == report.current_version
    assert notifier.attempts == 1


def test_an_unknown_report_is_a_not_found_failure(session: Session, workspace: Workspace) -> None:
    """Pushing a report that does not exist is a missing resource, not a validation error."""
    target = workspace.add_push_target()

    with pytest.raises(PushNotFound) as failure:
        push_report(session, report_id=987654321, channel="email", target_id=target.id)

    assert failure.value.code == "NOT_FOUND"


def test_a_target_outside_the_allowlist_is_refused(session: Session, workspace: Workspace) -> None:
    """The allowlist is the only way to reach a destination."""
    report = seed_pushable_report(session, workspace)

    with pytest.raises(PushTargetNotAllowed) as failure:
        push_report(session, report_id=report.id, channel="email", target_id=987654321)

    assert failure.value.code == "PUSH_TARGET_NOT_ALLOWED"
    assert _pushes(session, report.id) == []


def test_a_disabled_target_is_refused(session: Session, workspace: Workspace) -> None:
    """Switching a destination off must stop delivery to it."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target(enabled=False)

    with pytest.raises(PushTargetNotAllowed):
        push_report(session, report_id=report.id, channel="email", target_id=target.id)


def test_the_channel_must_match_the_target(session: Session, workspace: Workspace) -> None:
    """A caller cannot ask an email target to accept a webhook delivery."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target(channel="email", destination="ops@example.com")

    with pytest.raises(PushTargetNotAllowed) as failure:
        push_report(session, report_id=report.id, channel="webhook", target_id=target.id)

    assert "email" in str(failure.value)


def test_a_private_report_needs_a_target_cleared_for_private(
    session: Session,
    workspace: Workspace,
) -> None:
    """`PRIVATE` is the narrowest scope, so a destination must be cleared for it explicitly."""
    report = seed_pushable_report(session, workspace, visibility="PRIVATE")
    target = workspace.add_push_target(allow_private=False)

    with pytest.raises(PushTargetNotAllowed):
        push_report(session, report_id=report.id, channel="email", target_id=target.id)

    cleared = workspace.add_push_target(allow_private=True)
    outcome = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=cleared.id,
        notifier=StubChannel(),
    )

    assert outcome.delivered is True


def test_a_confidential_report_needs_an_approval_and_a_cleared_target(
    session: Session,
    workspace: Workspace,
) -> None:
    """`REQUIREMENTS.md` §7 reserves external delivery of sensitive material for review."""
    report = seed_pushable_report(session, workspace, sensitivity="CONFIDENTIAL")
    target = workspace.add_push_target(allow_confidential=True)

    with pytest.raises(PushApprovalRequired) as failure:
        push_report(session, report_id=report.id, channel="email", target_id=target.id)

    assert failure.value.code == "PUSH_APPROVAL_REQUIRED"
    assert _pushes(session, report.id) == []

    _approve(session, report)
    uncleared = workspace.add_push_target(allow_confidential=False)
    with pytest.raises(PushTargetNotAllowed):
        push_report(session, report_id=report.id, channel="email", target_id=uncleared.id)

    outcome = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        notifier=StubChannel(),
    )
    assert outcome.delivered is True


def test_delivery_is_recorded_with_its_run_and_audit(
    session: Session,
    workspace: Workspace,
    session_factory: sessionmaker[Session],
) -> None:
    """A delivery has to leave a trail: the row, the run, and the audit event."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target(destination="ops@example.com")

    outcome = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        actor_id=42,
        notifier=StubChannel(),
    )

    row = session.get(ReportPush, outcome.push_id)
    assert row is not None
    assert row.status == "sent"
    assert row.attempt == 1
    assert row.destination == "ops@example.com"
    assert row.sent_at is not None
    assert row.message_json is not None
    assert row.message_json["subject"].endswith(report.title)

    run = session.get(PipelineRun, row.pipeline_run_id)
    assert run is not None
    assert run.run_type == "push"
    assert run.status == "succeeded"
    assert run.topic_id == report.topic_id
    assert run.attempt == 1

    events = session.scalars(
        select(AuditLog).where(AuditLog.action == "report.push").order_by(AuditLog.id)
    ).all()
    assert [(event.result, event.actor_id) for event in events] == [("success", 42)]


def test_replaying_a_delivered_push_does_not_send_twice(
    session: Session,
    workspace: Workspace,
) -> None:
    """One (report, version, target) triple is delivered at most once."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    notifier = StubChannel()

    first = push_report(
        session, report_id=report.id, channel="email", target_id=target.id, notifier=notifier
    )
    second = push_report(
        session, report_id=report.id, channel="email", target_id=target.id, notifier=notifier
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.push_id == first.push_id
    assert second.attempt == 1
    assert notifier.attempts == 1
    assert len(_pushes(session, report.id)) == 1


def test_a_refused_delivery_is_recorded_and_retryable(
    session: Session,
    workspace: Workspace,
) -> None:
    """A transport failure has to survive as evidence so the retry sweep can pick it up."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    notifier = StubChannel(fail_times=1)

    failed = push_report(
        session, report_id=report.id, channel="email", target_id=target.id, notifier=notifier
    )

    assert failed.delivered is False
    assert failed.status == "failed"
    assert failed.error_code == "PUSH_DELIVERY_FAILED"
    assert failed.attempt == 1
    failed_run = session.get(PipelineRun, session.get(ReportPush, failed.push_id).pipeline_run_id)
    assert failed_run is not None and failed_run.status == "failed"

    retried = retry_push(session, push_id=failed.push_id, notifier=notifier)

    assert retried.delivered is True
    assert retried.push_id == failed.push_id
    assert retried.attempt == 2
    assert len(_pushes(session, report.id)) == 1
    assert notifier.attempts == 2


def test_the_retry_budget_is_enforced(session: Session, workspace: Workspace) -> None:
    """A permanently broken destination must eventually stop being retried."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    notifier = StubChannel(fail_times=99)
    settings = build_settings(push_max_attempts=2)

    first = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        notifier=notifier,
        settings=settings,
    )
    second = retry_push(session, push_id=first.push_id, notifier=notifier, settings=settings)

    assert (first.attempt, second.attempt) == (1, 2)
    assert second.delivered is False

    with pytest.raises(PushDeliveryFailed) as failure:
        retry_push(session, push_id=first.push_id, notifier=notifier, settings=settings)

    assert failure.value.code == "PUSH_DELIVERY_FAILED"
    assert notifier.attempts == 2


def test_retry_failed_pushes_sweeps_the_backlog(session: Session, workspace: Workspace) -> None:
    """The sweep retries every failed row that still has budget, and skips the rest."""
    first = seed_pushable_report(session, workspace)
    second = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    notifier = StubChannel(fail_times=2)

    for report in (first, second):
        push_report(
            session,
            report_id=report.id,
            channel="email",
            target_id=target.id,
            notifier=notifier,
        )
    assert len(list_pushes(session, status="failed")) == 2

    outcomes = retry_failed_pushes(session, notifier=notifier)

    assert len(outcomes) == 2
    assert all(item.delivered for item in outcomes)
    assert list_pushes(session, status="failed") == []
    assert {item.report_id for item in _pushes(session, first.id)} == {first.id}


def test_retry_failed_pushes_is_a_no_op_when_nothing_failed(
    session: Session,
    workspace: Workspace,
) -> None:
    """A sweep with no candidates must not touch anything."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        notifier=StubChannel(),
    )

    assert retry_failed_pushes(session, notifier=StubChannel()) == []


def test_pushing_can_be_switched_off(session: Session, workspace: Workspace) -> None:
    """`PUSH_ENABLED=False` must stop all delivery, including the retry sweep."""
    report = seed_pushable_report(session, workspace)
    target = workspace.add_push_target()
    settings = build_settings(push_enabled=False)

    with pytest.raises(PushDisabled) as failure:
        push_report(
            session,
            report_id=report.id,
            channel="email",
            target_id=target.id,
            settings=settings,
        )

    assert failure.value.code == "PUSH_DISABLED"
    assert retry_failed_pushes(session, settings=settings) == []
    assert push_after_publish(session, report_id=report.id, settings=settings) == []


def test_auto_push_after_publish_delivers_to_every_enabled_target(
    session: Session,
    workspace: Workspace,
) -> None:
    """A release fans out to the allowlist, and a disabled row is not contacted."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    workspace.add_push_target(destination="ops@example.com")
    workspace.add_push_target(channel="webhook", destination="https://hooks.example.com/osia")
    workspace.add_push_target(destination="off@example.com", enabled=False)

    outcomes = push_after_publish(session, report_id=report.id)

    assert {item.destination for item in outcomes} == {
        "ops@example.com",
        "https://hooks.example.com/osia",
    }
    assert all(item.delivered for item in outcomes)
    assert len(_pushes(session, report.id)) == 2


def test_auto_push_swallows_a_refused_target(
    session: Session,
    workspace: Workspace,
) -> None:
    """A release must survive a destination that is not cleared for the report."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED, sensitivity="CONFIDENTIAL")
    workspace.add_push_target(allow_confidential=False)

    outcomes = push_after_publish(session, report_id=report.id)

    assert outcomes == []
    assert _pushes(session, report.id) == []
    events = session.scalars(
        select(AuditLog).where(AuditLog.action == "report.push", AuditLog.result == "denied")
    ).all()
    assert len(events) == 1
    assert events[0].metadata_json is not None
    assert events[0].metadata_json["auto"] is True


def test_auto_push_respects_its_own_switch(session: Session, workspace: Workspace) -> None:
    """Operators can keep manual pushing while switching the automatic one off."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    workspace.add_push_target()
    settings = build_settings(push_auto_after_publish=False)

    assert push_after_publish(session, report_id=report.id, settings=settings) == []
    explicit = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=list_targets(session)[0].id,
        settings=settings,
        notifier=StubChannel(),
    )
    assert explicit.delivered is True


def test_the_message_carries_the_report_facts(session: Session, workspace: Workspace) -> None:
    """The notification has to identify which report and version a reader is looking at."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)

    message = build_message(session, report)

    assert message.report_id == report.id
    assert message.version == report.current_version
    assert report.title in message.subject
    assert workspace.session.get(type(report), report.id) is not None
    topic_name = session.get(Topic, report.topic_id).name  # type: ignore[union-attr]
    for fragment in ("版本：v1", "状态：published", f"主题：{topic_name}"):
        assert fragment in message.body


def test_the_offline_channel_is_deterministic() -> None:
    """The default transport has to be reproducible so local runs are comparable."""
    message = OutboundMessage(report_id=1, version=2, subject="s", body="b")
    channel = OfflineChannel("email")

    first = channel.send(destination="ops@example.com", message=message)
    second = channel.send(destination="ops@example.com", message=message)

    assert first == second
    assert first.ok is True
    assert first.detail is not None and first.detail.startswith("offline:email:")
    changed = OutboundMessage(report_id=1, version=2, subject="s", body="b2")
    assert channel.send(destination="ops@example.com", message=changed).detail != first.detail


def test_the_webhook_signature_is_stable() -> None:
    """A receiver has to be able to verify the payload with the shared secret."""
    assert sign_body("secret", b"{}") == sign_body("secret", b"{}")
    assert sign_body("secret", b"{}") != sign_body("other", b"{}")
    assert sign_body("secret", b"{}").startswith("sha256=")


@pytest.mark.parametrize(
    "destination",
    (
        "http://hooks.example.com/osia",
        "https://user:password@hooks.example.com/osia",
        "ftp://hooks.example.com/osia",
    ),
)
def test_webhook_url_requires_https_without_embedded_credentials(destination: str) -> None:
    """Webhook destinations cannot smuggle a downgrade or URL credentials."""
    with pytest.raises(PushTargetNotAllowed):
        validate_webhook_url(destination)


def test_webhook_destination_rejects_private_addresses() -> None:
    """A destination resolving to loopback must never reach an outbound request."""
    with pytest.raises(PushTargetNotAllowed, match="non-public"):
        validate_webhook_destination("https://127.0.0.1/internal")


def test_failed_push_details_are_redacted_before_persistence(
    session: Session,
    workspace: Workspace,
) -> None:
    """Transport diagnostics must not leave credentials in the delivery table."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target(destination="ops@example.com")
    notifier = StubChannel(fail_times=1, detail="token=super-secret-token")

    outcome = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        notifier=notifier,
    )
    row = session.get(ReportPush, outcome.push_id)

    assert row is not None
    assert row.response_excerpt is not None
    assert "super-secret-token" not in row.response_excerpt
    assert row.error_message is not None
    assert "super-secret-token" not in row.error_message


def test_retry_does_not_send_a_superseded_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """A retry row is for its original version; a newer version must supersede it."""
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    target = workspace.add_push_target(destination="ops@example.com")
    settings = build_settings(push_max_attempts=3)
    notifier = StubChannel(fail_times=1)
    failed = push_report(
        session,
        report_id=report.id,
        channel="email",
        target_id=target.id,
        settings=settings,
        notifier=notifier,
    )
    report.current_version = 2
    session.commit()

    outcomes = retry_failed_pushes(session, settings=settings, notifier=notifier)
    row = session.get(ReportPush, failed.push_id)

    assert outcomes == []
    assert notifier.attempts == 1
    assert row is not None
    assert row.error_code == PUSH_VERSION_SUPERSEDED
    assert row.attempt == settings.push_max_attempts


def test_build_channel_needs_credentials_for_live_email() -> None:
    """Live delivery without a sender must fail loudly rather than silently pretend to send."""
    offline = build_channel(build_settings(), "email")
    assert isinstance(offline, OfflineChannel)

    with pytest.raises(PushTargetNotAllowed):
        build_channel(build_settings(push_delivery="live"), "email")

    live = build_channel(
        build_settings(
            push_delivery="live",
            push_smtp_host="smtp.example.com",
            push_email_from="reports@example.com",
        ),
        "email",
    )
    assert live.channel == "email"


def test_build_channel_refuses_an_unknown_channel() -> None:
    """The channel set is closed."""
    with pytest.raises(PushTargetNotAllowed):
        build_channel(build_settings(), "carrier_pigeon")


def test_private_webhook_target_needs_the_test_escape_hatch() -> None:
    """A loopback destination stays refused until the test-only switch flips it."""
    with pytest.raises(PushTargetNotAllowed, match="non-public"):
        validate_webhook_destination("https://127.0.0.1:8443/hook")

    assert validate_webhook_destination("https://127.0.0.1:8443/hook", allow_private=True) is None


def test_private_webhook_escape_is_refused_outside_local_and_test() -> None:
    """The SSRF guard must never be widened in staging or production."""
    for env in ("staging", "production"):
        with pytest.raises(ValueError, match="only allowed in local and test"):
            build_settings(app_env=env, push_webhook_allow_private_targets=True)
        with pytest.raises(ValueError, match="only allowed in local and test"):
            build_settings(app_env=env, push_webhook_ca_bundle="/tmp/ca.pem")


def test_live_webhook_channel_carries_the_test_escape_hatches() -> None:
    """`build_channel` hands the private-address and CA-bundle switches to the adapter."""
    live = build_channel(
        build_settings(
            push_delivery="live",
            push_webhook_allow_private_targets=True,
            push_webhook_ca_bundle="/tmp/ca.pem",
        ),
        "webhook",
    )
    assert live.channel == "webhook"
    assert live._allow_private is True  # noqa: SLF001 - asserting the adapter wiring
    assert live._verify == "/tmp/ca.pem"  # noqa: SLF001 - asserting the adapter wiring
