"""Manual edit and rollback: state guards, version bookkeeping, and citation copying.

`tests/web/test_web.py` drives these services through the UI, but only from `published`.
The guards themselves were never pinned, which is how a rollback from `scheduled` slipped
through and mutated the report before the state machine refused the transition.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from reports_support import AUTO, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.reports.errors import (
    ReportGenerationError,
    ReportNotFoundError,
    ReportStateError,
)
from open_source_intelligence.reports.revisions import (
    EDITABLE_STATUSES,
    ROLLBACKABLE_STATUSES,
    revise_report,
    rollback_report,
)
from open_source_intelligence.reports.state import ALLOWED_TRANSITIONS, transition_report

#: Statuses a report can be parked in that must refuse a rollback, with the legal path to
#: reach each one. `failed` is covered separately because it is the terminal branch.
PUBLISHING_PATH = (
    ("pending_review", ("pending_review",)),
    ("approved", ("pending_review", "approved")),
    ("scheduled", ("scheduled",)),
)


def _advance(session: Session, report_id: int, *targets: str) -> Report:
    """Drive a report through the real state machine and return the row."""
    report = session.get(Report, report_id)
    assert report is not None
    for target in targets:
        transition_report(report, target)
    session.commit()
    return report


def _revise(session: Session, case: SeededReport) -> Report:
    """Apply one content edit so the report carries a second version."""
    report = case.reload(session)
    revise_report(
        session,
        report_id=case.report_id,
        content_markdown=report.content_markdown + "\n\n补充说明\n",
        now=NOW + timedelta(minutes=1),
    )
    return case.reload(session)


def _version_rows(session: Session, report_id: int) -> int:
    """Return how many versions are persisted for a report."""
    return len(
        session.scalars(select(ReportVersion).where(ReportVersion.report_id == report_id)).all()
    )


def test_rollbackable_statuses_match_the_state_machine() -> None:
    """The declared set must equal the statuses whose legal moves include `draft`."""
    derived = {status for status, targets in ALLOWED_TRANSITIONS.items() if "draft" in targets}
    derived.add("draft")  # `transition_report` treats draft -> draft as a no-op
    assert ROLLBACKABLE_STATUSES == frozenset(derived)
    assert "failed" not in ROLLBACKABLE_STATUSES


def test_editable_statuses_are_the_draft_queue() -> None:
    """Only the draft queue may be rewritten by hand."""
    assert EDITABLE_STATUSES == frozenset({"draft", "changes_requested"})
    assert EDITABLE_STATUSES <= ROLLBACKABLE_STATUSES


@pytest.mark.parametrize(("label", "targets"), PUBLISHING_PATH)
def test_rollback_is_refused_without_touching_the_report(
    session: Session,
    workspace: Workspace,
    label: str,
    targets: tuple[str, ...],
) -> None:
    """A refused rollback leaves both the object and the database untouched.

    This is the regression for the ordering bug: the service used to append the version
    first and consult the state machine last, so a refusal arrived after `current_version`
    had already been bumped and the body replaced on the very object the UI renders.
    """
    case = build_report(session, workspace, publish_mode=AUTO)
    _revise(session, case)
    report = _advance(session, case.report_id, *targets)
    assert report.status == label

    before_version = report.current_version
    before_content = report.content_markdown
    before_rows = _version_rows(session, case.report_id)

    with pytest.raises(ReportStateError):
        rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    # The identity-mapped object the service mutated; asserting without a fresh query is the
    # point, because that object is exactly what the UI panel renders after catching the error.
    assert report.status == label
    assert report.current_version == before_version
    assert report.content_markdown == before_content
    assert _version_rows(session, case.report_id) == before_rows


def test_rollback_refuses_the_terminal_failed_status(
    session: Session,
    workspace: Workspace,
) -> None:
    """`failed` is terminal: a rejected report is never revived by a rollback."""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    _advance(session, case.report_id, "failed")
    before_version = report.current_version

    with pytest.raises(ReportStateError):
        rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert report.status == "failed"
    assert report.current_version == before_version


def test_rollback_from_draft_restores_an_earlier_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """Rewinding a draft appends a new version and copies that version's citations."""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    before_version = report.current_version
    first_content = session.scalar(
        select(ReportVersion.content_markdown).where(
            ReportVersion.report_id == case.report_id,
            ReportVersion.version == 1,
        )
    )
    assert first_content is not None
    source_rows_before = len(
        session.scalars(
            select(ReportSource).where(
                ReportSource.report_id == case.report_id,
                ReportSource.version == 1,
            )
        ).all()
    )

    outcome = rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert outcome.restored_from == 1
    assert outcome.version == before_version + 1
    assert outcome.status == "draft"
    assert report.content_markdown == first_content
    assert report.published_at is None
    assert report.publish_at is None
    # The restored version carries its own copy of the citation set.
    restored = session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == case.report_id,
            ReportSource.version == outcome.version,
        )
    ).all()
    assert len(restored) == source_rows_before


def test_rollback_from_published_withdraws_the_report(
    session: Session,
    workspace: Workspace,
) -> None:
    """A published report can be pulled back to `draft`."""
    case = build_report(session, workspace, publish_mode=AUTO)
    _revise(session, case)
    _advance(session, case.report_id, "scheduled", "published")

    outcome = rollback_report(session, report_id=case.report_id, version=1, now=NOW)

    assert outcome.status == "draft"
    report = case.reload(session)
    assert report.published_at is None


def test_rollback_rejects_the_current_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """Rolling back to the version already in place is not a revision."""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = _revise(session, case)
    with pytest.raises(ReportGenerationError):
        rollback_report(session, report_id=case.report_id, version=report.current_version)


def test_rollback_rejects_an_unknown_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """A version that was never written cannot be restored."""
    case = build_report(session, workspace, publish_mode=AUTO)
    with pytest.raises(ReportNotFoundError):
        rollback_report(session, report_id=case.report_id, version=99)


def test_content_edit_appends_a_version_and_returns_to_draft(
    session: Session,
    workspace: Workspace,
) -> None:
    """A body change is immutable by definition, so it appends and re-enters the draft queue."""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    original = report.content_markdown

    outcome = revise_report(
        session,
        report_id=case.report_id,
        content_markdown=original + "\n\n新增段落\n",
        now=NOW,
    )

    assert outcome.content_changed is True
    assert outcome.version == 2
    assert outcome.status == "draft"
    assert _version_rows(session, case.report_id) == 2


def test_metadata_edit_does_not_append_a_version(
    session: Session,
    workspace: Workspace,
) -> None:
    """Editing only metadata keeps the current version and its status."""
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    before_version = report.current_version

    outcome = revise_report(
        session,
        report_id=case.report_id,
        visibility="PRIVATE",
        now=NOW,
    )

    assert outcome.content_changed is False
    assert outcome.version == before_version
    assert outcome.changed_fields == ("visibility",)
    assert report.visibility == "PRIVATE"
    assert report.owner_id is not None


def test_edit_is_refused_outside_the_draft_queue(
    session: Session,
    workspace: Workspace,
) -> None:
    """A report on the publishing path cannot be rewritten by hand."""
    case = build_report(session, workspace, publish_mode=AUTO)
    _advance(session, case.report_id, "scheduled")

    with pytest.raises(ReportStateError):
        revise_report(session, report_id=case.report_id, title="改标题", now=NOW)


def test_edit_refuses_an_unknown_visibility(
    session: Session,
    workspace: Workspace,
) -> None:
    """Visibility is a closed set; an unknown value never reaches the column."""
    case = build_report(session, workspace, publish_mode=AUTO)
    with pytest.raises(ReportGenerationError):
        revise_report(session, report_id=case.report_id, visibility="SECRET", now=NOW)


@pytest.mark.parametrize("bad_title", ["", "   ", "第一行\n第二行", "x" * 256])
def test_edit_refuses_a_title_that_cannot_render_as_one_heading(
    session: Session,
    workspace: Workspace,
    bad_title: str,
) -> None:
    """A blank, multi-line, or over-long title is rejected instead of stored.

    The structure check compares the first body line against `# <title>`, so a newline would
    leave the report failing verification with the reason looking like a content problem.
    """
    case = build_report(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    before_title = report.title

    with pytest.raises(ReportGenerationError):
        revise_report(session, report_id=case.report_id, title=bad_title, now=NOW)

    assert report.title == before_title


def test_edit_trims_a_title(session: Session, workspace: Workspace) -> None:
    """Surrounding whitespace is normalized rather than persisted."""
    case = build_report(session, workspace, publish_mode=AUTO)

    outcome = revise_report(session, report_id=case.report_id, title="  新标题  ", now=NOW)

    assert outcome.changed_fields == ("title",)
    assert case.reload(session).title == "新标题"
