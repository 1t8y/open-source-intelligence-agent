"""Tests for the no-login share link: the sensitivity gate, the token digest, and expiry.

The property that carries the security weight is not "a link works" but "only a report that is
allowed to leave the building can get one, and the row cannot be turned back into a link". Both
are asserted directly against the database rather than through the service's return value.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink
from open_source_intelligence.reports.sharing import (
    DEFAULT_SHARE_TTL_HOURS,
    MAX_SHARE_TTL_HOURS,
    ShareError,
    ShareLinkNotFound,
    ShareNotPublished,
    ShareSensitivityRefused,
    create_share_link,
    hash_share_token,
    list_share_links,
    resolve_share_token,
    revoke_share_link,
)


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """Seed a report in a chosen publishing state and sensitivity.

    The row is constructed in its final state rather than transitioned into it: these tests are
    about the share gate, and driving the publish policy would make them fail for reasons that
    have nothing to do with sharing.
    """
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="分享用周报",
        current_version=1,
        content_markdown="# 分享用周报\n\n正文。\n",
        status=status,
        visibility="INTERNAL",
        sensitivity=sensitivity,
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW if status == "published" else None,
    )
    session.add(report)
    session.commit()
    return report


def test_a_public_published_report_gets_a_link(session: Session, workspace: Workspace) -> None:
    """The happy path: a token comes back, and it is long enough to be unguessable."""
    report = seed_report(session, workspace)

    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert created.url_path == f"/share/{created.token}"
    assert len(created.token) >= 43
    assert created.expires_at == NOW + timedelta(hours=DEFAULT_SHARE_TTL_HOURS)


def test_only_the_digest_is_stored(session: Session, workspace: Workspace) -> None:
    """A database that cannot store the plaintext cannot leak it.

    This is the whole reason the table exists in this shape, so it is asserted against the row
    itself: if a future change adds a plaintext column "for convenience", this fails.
    """
    report = seed_report(session, workspace)

    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    row = session.scalar(select(ReportShareLink).where(ReportShareLink.id == created.share_id))
    assert row is not None
    assert row.token_hash == hashlib.sha256(created.token.encode("utf-8")).hexdigest()
    assert row.token_hash == hash_share_token(created.token)
    assert created.token not in repr(row.__dict__)


def test_listing_never_returns_a_token(session: Session, workspace: Workspace) -> None:
    """The list payload has no field that could carry the plaintext."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    summaries = list_share_links(session, report_id=report.id)

    assert [summary.share_id for summary in summaries] == [created.share_id]
    payload = summaries[0].to_dict()
    assert "token" not in payload
    assert created.token not in str(payload)


@pytest.mark.parametrize("sensitivity", ["INTERNAL", "CONFIDENTIAL"])
def test_a_non_public_report_is_refused(
    session: Session, workspace: Workspace, sensitivity: str
) -> None:
    """The gate is sensitivity, not visibility.

    `visibility` is INTERNAL on every one of these reports - readable by any logged-in user,
    which reads as "harmless" right up until a link makes it readable by anyone with the URL.
    """
    report = seed_report(session, workspace, sensitivity=sensitivity)

    with pytest.raises(ShareSensitivityRefused) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert excinfo.value.code == "SHARE_SENSITIVITY_REFUSED"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_a_public_report_that_is_not_published_is_refused(
    session: Session, workspace: Workspace
) -> None:
    """A draft is not a statement the organisation has agreed to."""
    report = seed_report(session, workspace, status="draft", sensitivity="PUBLIC")

    with pytest.raises(ShareNotPublished) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert excinfo.value.code == "SHARE_NOT_PUBLISHED"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_a_valid_token_resolves_and_counts_the_visit(
    session: Session, workspace: Workspace
) -> None:
    """Resolution returns the report and records usage without touching the token."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    resolved = resolve_share_token(session, created.token, now=NOW)

    assert resolved is not None
    assert resolved.report.id == report.id
    row = session.get(ReportShareLink, created.share_id)
    assert row is not None
    assert row.access_count == 1
    assert row.last_accessed_at is not None


def test_an_unknown_token_resolves_to_nothing(session: Session, workspace: Workspace) -> None:
    """No row means no answer, and the route turns every `None` into one 404."""
    report = seed_report(session, workspace)
    create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert resolve_share_token(session, "not-a-real-token", now=NOW) is None


def test_a_revoked_link_stops_resolving(session: Session, workspace: Workspace) -> None:
    """Revocation is immediate: the next read fails, with no cache to wait out."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    assert resolve_share_token(session, created.token, now=NOW) is None


def test_revoking_twice_is_idempotent(session: Session, workspace: Workspace) -> None:
    """The intent ("make sure it stops working") is already satisfied, so no error."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    first = revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    second = revoke_share_link(
        session,
        report_id=report.id,
        share_id=created.share_id,
        actor_id=None,
        now=NOW + timedelta(hours=1),
    )

    assert first is True
    assert second is False
    row = session.get(ReportShareLink, created.share_id)
    assert row is not None
    assert row.revoked_at == NOW


def test_revoking_a_link_of_another_report_is_not_found(
    session: Session, workspace: Workspace
) -> None:
    """A share id is scoped to its report, so a guessed id cannot be revoked through another."""
    report = seed_report(session, workspace)
    other = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    with pytest.raises(ShareLinkNotFound):
        revoke_share_link(
            session, report_id=other.id, share_id=created.share_id, actor_id=None, now=NOW
        )


def test_an_expired_link_stops_resolving(session: Session, workspace: Workspace) -> None:
    """The clock is a parameter, so "expired" is tested without waiting."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, ttl_hours=1, now=NOW)

    assert resolve_share_token(session, created.token, now=NOW + timedelta(minutes=30)) is not None
    assert resolve_share_token(session, created.token, now=NOW + timedelta(hours=2)) is None


def test_a_link_with_no_expiry_never_expires(session: Session, workspace: Workspace) -> None:
    """`ttl_hours=None` is an explicit choice, not a missing value."""
    report = seed_report(session, workspace)
    created = create_share_link(
        session, report_id=report.id, actor_id=None, ttl_hours=None, now=NOW
    )

    assert created.expires_at is None
    assert resolve_share_token(session, created.token, now=NOW + timedelta(days=3650)) is not None


def test_a_report_that_stops_being_public_kills_its_link(
    session: Session, workspace: Workspace
) -> None:
    """Reclassifying the content takes effect on the next read, not on the next link.

    This is why the report-side conditions are re-checked at resolution time instead of being
    frozen when the link is minted.
    """
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    report.sensitivity = "INTERNAL"
    session.commit()

    assert resolve_share_token(session, created.token, now=NOW) is None


def test_a_retracted_report_kills_its_link(session: Session, workspace: Workspace) -> None:
    """Withdrawing a report must withdraw the address that points at it."""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    report.status = "draft"
    session.commit()

    assert resolve_share_token(session, created.token, now=NOW) is None


@pytest.mark.parametrize("ttl_hours", [0, -1, MAX_SHARE_TTL_HOURS + 1])
def test_a_lifetime_outside_the_bounds_is_refused(
    session: Session, workspace: Workspace, ttl_hours: int
) -> None:
    """A bad lifetime is a caller mistake and must not mint a row."""
    report = seed_report(session, workspace)

    with pytest.raises(ShareError) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, ttl_hours=ttl_hours, now=NOW)

    assert excinfo.value.code == "SHARE_ERROR"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_minting_and_revoking_are_audited(session: Session, workspace: Workspace) -> None:
    """The operator's action is the only thing anyone can be held to later, so it is recorded."""
    from open_source_intelligence.persistence.models.audit import AuditLog

    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.resource_id == str(report.id))
    ).all()
    assert "report.share_create" in actions
    assert "report.share_revoke" in actions


def test_reading_a_share_is_not_audited(session: Session, workspace: Workspace) -> None:
    """Anonymous reads have no actor to attribute, so they must not flood the audit table."""
    from open_source_intelligence.persistence.models.audit import AuditLog

    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    before = len(session.scalars(select(AuditLog)).all())

    resolve_share_token(session, created.token, now=NOW)
    resolve_share_token(session, created.token, now=NOW)

    after = len(session.scalars(select(AuditLog)).all())
    assert after == before
