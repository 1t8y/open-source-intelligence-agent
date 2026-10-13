"""Report revision operations: manual edits and rollback to a historical version.

Both operations append a new immutable `report_versions` row and (for a content change)
return the report to `draft`, because a changed artifact has to be verified and published
again before anyone sees it. They differ only in where the content comes from: an edit takes
it from the caller, a rollback takes it from a version that was already released once.

Every new version inherits the citation set of the version it replaces. That keeps an edited
report eligible for publication while `INV-10` stays enforceable: the Verifier re-resolves
each inherited reference against the database, so a broken reference or a section that states
a number without a metric citation still blocks the release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import (
    REPORT_SENSITIVITIES,
    REPORT_VISIBILITIES,
    Report,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.security.audit import record_audit_event

from .errors import ReportGenerationError, ReportNotFoundError, ReportStateError
from .state import transition_report

#: Statuses in which a report's content may be rewritten by hand. This mirrors the rule
#: `generate_report` already applies, so both edit paths agree on when a report is editable.
EDITABLE_STATUSES: Final[frozenset[str]] = frozenset({"draft", "changes_requested"})

#: Longest title accepted by the edit paths; mirrors `Report.title`'s column width.
MAX_TITLE_CHARS: Final[int] = 255

#: Statuses a rollback may return to `draft`. It mirrors the legal edges *into* `draft`:
#: `draft` and `changes_requested` can be rewound to an earlier version, and `published` can
#: be withdrawn. A report still on the publishing path (`pending_review`, `approved`,
#: `scheduled`) has to leave that path explicitly first, and `failed` is terminal.
ROLLBACKABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {"draft", "changes_requested", "published"}
)


class _Unset:
    """Sentinel type that tells `publish_at=None` apart from "leave it unchanged"."""


_UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class RevisionOutcome:
    """Result of one edit or rollback."""

    report_id: int
    version: int
    status: str
    content_changed: bool
    changed_fields: tuple[str, ...]
    restored_from: int | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload."""
        return {
            "report_id": self.report_id,
            "version": self.version,
            "status": self.status,
            "content_changed": self.content_changed,
            "changed_fields": list(self.changed_fields),
            "restored_from": self.restored_from,
        }


def _require_report(session: Session, report_id: int) -> Report:
    """Load a report or fail with a stable error."""
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
    return report


def _normalize_title(title: str) -> str:
    """Return a trimmed title, rejecting anything that cannot render as one heading.

    `verification._check_structure` compares the first body line against `# <title>`, so a
    title carrying a newline could never satisfy the structure check: the report would sit at
    `failed` verification with no way to publish it, and the reason would look like a content
    problem rather than a bad field.
    """
    normalized = title.strip()
    if (
        not normalized
        or len(normalized) > MAX_TITLE_CHARS
        or "\n" in normalized
        or "\r" in normalized
    ):
        raise ReportGenerationError(
            f"title must be a single non-empty line of at most {MAX_TITLE_CHARS} characters"
        )
    return normalized


def _copy_sources(
    session: Session,
    report: Report,
    *,
    from_version: int,
    to_version: int,
    moment: datetime,
) -> int:
    """Copy one version's citation set onto another and return how many rows moved."""
    session.execute(
        delete(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == to_version,
        )
    )
    rows = session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == report.id,
            ReportSource.version == from_version,
        )
    ).all()
    for row in rows:
        session.add(
            ReportSource(
                report_id=report.id,
                version=to_version,
                repository_id=row.repository_id,
                section_key=row.section_key,
                claim_text=row.claim_text,
                source_type=row.source_type,
                source_url=row.source_url,
                source_ref=row.source_ref,
                metric_snapshot_id=row.metric_snapshot_id,
                created_at=moment,
            )
        )
    return len(rows)


def _append_version(
    session: Session,
    report: Report,
    *,
    content: str,
    actor_id: int | None,
    change_reason: str,
    moment: datetime,
) -> int:
    """Append the next immutable version and return its number."""
    version = report.current_version + 1
    report.current_version = version
    report.content_markdown = content
    session.add(
        ReportVersion(
            report_id=report.id,
            version=version,
            content_markdown=content,
            created_by=actor_id,
            change_reason=change_reason,
            created_at=moment,
        )
    )
    return version


def _return_to_draft(report: Report) -> None:
    """Put a report back in the editing queue and clear the publish window it lost."""
    transition_report(report, "draft")
    report.publish_at = None
    report.published_at = None


def revise_report(
    session: Session,
    *,
    report_id: int,
    actor_id: int | None = None,
    title: str | None = None,
    content_markdown: str | None = None,
    visibility: str | None = None,
    sensitivity: str | None = None,
    publish_at: datetime | None | _Unset = _UNSET,
    now: datetime | None = None,
) -> RevisionOutcome:
    """Apply one manual edit, appending a version when the content changes.

    Metadata-only edits update the row in place; a content edit is immutable by definition,
    so it appends a version and returns the report to `draft` for re-verification.
    """
    moment = now or utc_now()
    report = _require_report(session, report_id)
    if report.status not in EDITABLE_STATUSES:
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a draft or a report returned for "
            "changes can be edited"
        )
    if visibility is not None and visibility not in REPORT_VISIBILITIES:
        raise ReportGenerationError(f"unsupported visibility: {visibility}")
    if sensitivity is not None and sensitivity not in REPORT_SENSITIVITIES:
        raise ReportGenerationError(f"unsupported sensitivity: {sensitivity}")
    if not isinstance(publish_at, _Unset) and publish_at is not None and publish_at.tzinfo is None:
        raise ReportGenerationError("publish_at must be a timezone-aware UTC timestamp")
    if title is not None:
        title = _normalize_title(title)

    changed: list[str] = []
    if title is not None and title != report.title:
        report.title = title
        changed.append("title")
    if visibility is not None and visibility != report.visibility:
        report.visibility = visibility
        changed.append("visibility")
    if sensitivity is not None and sensitivity != report.sensitivity:
        report.sensitivity = sensitivity
        changed.append("sensitivity")
    if not isinstance(publish_at, _Unset) and publish_at != report.publish_at:
        report.publish_at = publish_at
        changed.append("publish_at")

    content_changed = content_markdown is not None and content_markdown != report.content_markdown
    version = report.current_version
    if content_changed:
        assert content_markdown is not None
        previous_version = report.current_version
        # Return the report to `draft` before the new version is written, mirroring
        # `rollback_report`: the states that allow an edit are exactly the states that may
        # enter `draft`, and deciding that first keeps a refused transition from leaving a
        # half-applied revision behind.
        _return_to_draft(report)
        version = _append_version(
            session,
            report,
            content=content_markdown,
            actor_id=actor_id,
            change_reason="manual_edit",
            moment=moment,
        )
        _copy_sources(
            session,
            report,
            from_version=previous_version,
            to_version=version,
            moment=moment,
        )
        changed.append("content_markdown")

    if changed:
        record_audit_event(
            session,
            action="report.edit",
            resource_type="report",
            resource_id=str(report.id),
            result="success",
            actor_id=actor_id,
            metadata_json={
                "version": version,
                "changed_fields": changed,
                "content_changed": content_changed,
            },
        )
    session.commit()
    return RevisionOutcome(
        report_id=report.id,
        version=version,
        status=report.status,
        content_changed=content_changed,
        changed_fields=tuple(changed),
        restored_from=None,
    )


def rollback_report(
    session: Session,
    *,
    report_id: int,
    version: int,
    actor_id: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> RevisionOutcome:
    """Restore a historical version as a new draft version.

    The restored content and its citation set are copied onto a *new* version rather than
    rewinding `current_version`, so the version history stays append-only and the report
    still has to clear verification and the publish policy again.
    """
    moment = now or utc_now()
    report = _require_report(session, report_id)
    # The state check comes before any write. `_return_to_draft` can refuse the transition,
    # and a refusal has to leave the report exactly as it was: checking afterwards would hand
    # the caller an error while the session already held a half-applied revision — a bumped
    # `current_version`, a replaced body, and staged `report_versions` / `report_sources`
    # rows — which the UI then renders on the same identity-mapped object.
    if report.status not in ROLLBACKABLE_STATUSES:
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a draft, a report returned for "
            "changes, or a published report can be rolled back"
        )
    target = session.scalar(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version == version,
        )
    )
    if target is None:
        raise ReportNotFoundError(f"report {report.id} has no version {version}")
    if version == report.current_version:
        raise ReportGenerationError(f"version {version} is already the current version")

    previous_version = report.current_version
    # Move the state machine first, for the same reason as the check above: once the version
    # row exists the revision has happened, so the transition has to be decided before it.
    _return_to_draft(report)
    new_version = _append_version(
        session,
        report,
        content=target.content_markdown,
        actor_id=actor_id,
        change_reason=f"rollback_to_v{version}",
        moment=moment,
    )
    restored_sources = _copy_sources(
        session,
        report,
        from_version=version,
        to_version=new_version,
        moment=moment,
    )
    record_audit_event(
        session,
        action="report.rollback",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        actor_id=actor_id,
        metadata_json={
            "restored_version": version,
            "from_version": previous_version,
            "to_version": new_version,
            "restored_sources": restored_sources,
            "comment": comment,
        },
    )
    session.commit()
    return RevisionOutcome(
        report_id=report.id,
        version=new_version,
        status=report.status,
        content_changed=True,
        changed_fields=("content_markdown",),
        restored_from=version,
    )
