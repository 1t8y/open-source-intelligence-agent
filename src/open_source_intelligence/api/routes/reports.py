"""Report endpoints: list, read, edit, verify, review, publish, and roll back.

Every route delegates to the `reports` services and to the authorization service; the router
only shapes requests and responses. Permission filtering for the list happens in the database
through `readable_reports_filter`, as `docs/api-contract.md` §6 requires: an unauthorized
report must never influence the page contents or the reported total.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.notifications.service import push_report
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.reports import queries
from open_source_intelligence.reports.publishing import (
    PUBLISH_BATCH_LIMIT,
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
)
from open_source_intelligence.reports.revisions import revise_report, rollback_report
from open_source_intelligence.reports.verification import verify_report
from open_source_intelligence.security.authorization import (
    REPORT_READ_ROLES,
    REPORT_WRITE_ROLES,
    authorize_report_read,
    readable_reports_filter,
)
from open_source_intelligence.security.dependencies import get_db_session, require_roles
from open_source_intelligence.security.service import get_role_codes

from ..params import UtcDateTime
from .push import PushOutcomeResponse

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

ReadUser = Annotated[User, Depends(require_roles(*REPORT_READ_ROLES))]
WriteUser = Annotated[User, Depends(require_roles(*REPORT_WRITE_ROLES))]
DbSession = Annotated[Session, Depends(get_db_session)]

ReportStatus = Literal[
    "draft",
    "pending_review",
    "approved",
    "scheduled",
    "published",
    "failed",
    "changes_requested",
]
ReportVisibility = Literal["PRIVATE", "TEAM", "INTERNAL"]
ReportSensitivity = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
ReviewDecision = Literal["approve", "reject", "request_changes"]


def _iso(value: datetime | None) -> str | None:
    """Render a timestamp the way the rest of the report services do."""
    return value.isoformat() if value else None


class ReportSummaryResponse(BaseModel):
    """One report row in a list response."""

    id: int
    agent_run_id: int
    topic_id: int
    topic_name: str | None
    owner_id: int | None
    title: str
    current_version: int
    status: str
    visibility: str
    sensitivity: str
    publish_mode: str
    publish_at: str | None
    generated_at: str
    published_at: str | None


class ReportListResponse(BaseModel):
    """Paginated report list."""

    items: list[ReportSummaryResponse]
    page: int
    page_size: int
    total: int


class ReportVersionResponse(BaseModel):
    """One immutable report version."""

    id: int
    version: int
    change_reason: str | None
    created_by: int | None
    created_at: str


class ReportSourceResponse(BaseModel):
    """One traceable citation of the current version."""

    id: int
    version: int
    section_key: str
    source_type: str
    source_ref: str | None
    source_url: str | None
    repository_id: int | None
    metric_snapshot_id: int | None


class ReportReviewResponse(BaseModel):
    """One human review decision."""

    id: int
    version: int
    reviewer_id: int | None
    decision: str
    comment: str | None
    created_at: str


class VerificationResponse(BaseModel):
    """The Verifier verdict stored for the current version, if it has been checked."""

    version: int
    verdict: str
    structure_ok: bool
    citations_ok: bool
    numbers_ok: bool
    sensitivity_ok: bool
    reason_codes: list[str]
    details: dict[str, Any] | None
    created_at: str


class ReportDetailResponse(BaseModel):
    """Full report detail including versions, citations, verdict, and reviews."""

    id: int
    agent_run_id: int
    topic_id: int
    topic_name: str | None
    owner_id: int | None
    title: str
    content_markdown: str
    current_version: int
    status: str
    visibility: str
    sensitivity: str
    publish_mode: str
    publish_at: str | None
    generated_at: str
    published_at: str | None
    versions: list[ReportVersionResponse]
    sources: list[ReportSourceResponse]
    verification: VerificationResponse | None
    reviews: list[ReportReviewResponse]


class ReportPatchRequest(BaseModel):
    """Editable report fields. Omitted fields are left untouched."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_markdown: str | None = Field(default=None, min_length=1)
    visibility: ReportVisibility | None = None
    sensitivity: ReportSensitivity | None = None
    publish_at: datetime | None = None


class ReviewRequest(BaseModel):
    """One human review decision submitted for a report awaiting review."""

    decision: ReviewDecision
    comment: str | None = Field(default=None, max_length=2000)
    edited_version_id: int | None = None


class RollbackRequest(BaseModel):
    """Request to restore a historical version as a new draft version."""

    version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class PushRequest(BaseModel):
    """Request to deliver one report version to one allowlisted target."""

    channel: Literal["email", "webhook"]
    target_id: int = Field(ge=1)


class RevisionResponse(BaseModel):
    """Result of an edit or a rollback."""

    report_id: int
    version: int
    status: str
    content_changed: bool
    changed_fields: list[str]
    restored_from: int | None


class ReviewResponse(BaseModel):
    """Result of recording a review decision."""

    report_id: int
    review_id: int
    decision: str
    status: str


class VerificationOutcomeResponse(BaseModel):
    """Result of running the Verifier."""

    report_id: int
    version: int
    verdict: str
    structure_ok: bool
    citations_ok: bool
    numbers_ok: bool
    sensitivity_ok: bool
    citation_coverage: float
    reason_codes: list[str]
    missing_headings: list[str]
    unresolved_refs: list[str]
    sensitive_markers: int


class PublishDecisionResponse(BaseModel):
    """The policy verdict returned by the publish endpoint."""

    report_id: int
    mode: str
    resolution: str
    reason_codes: list[str]
    requires_review: bool
    publish_at: str | None


class PublishResponse(BaseModel):
    """Result of applying the publish policy to one report."""

    report_id: int
    status: str
    pipeline_run_id: int | None
    publish_decision: PublishDecisionResponse
    verification: VerificationOutcomeResponse


class PublishDueResponse(BaseModel):
    """Result of one release sweep."""

    released: list[dict[str, Any]]
    released_count: int
    blocked_count: int


def _topic_names(session: Session, topic_ids: set[int]) -> dict[int, str]:
    """Return the names of the given topics in one query."""
    if not topic_ids:
        return {}
    rows = session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _summary(report: Any, *, topic_name: str | None) -> ReportSummaryResponse:
    """Build one list row."""
    return ReportSummaryResponse(
        id=report.id,
        agent_run_id=report.agent_run_id,
        topic_id=report.topic_id,
        topic_name=topic_name,
        owner_id=report.owner_id,
        title=report.title,
        current_version=report.current_version,
        status=report.status,
        visibility=report.visibility,
        sensitivity=report.sensitivity,
        publish_mode=report.publish_mode,
        publish_at=_iso(report.publish_at),
        generated_at=report.generated_at.isoformat(),
        published_at=_iso(report.published_at),
    )


@router.get("", response_model=ReportListResponse)
def list_reports(
    user: ReadUser,
    session: DbSession,
    topic_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[ReportStatus | None, Query(alias="status")] = None,
    visibility: Annotated[ReportVisibility | None, Query()] = None,
    sensitivity: Annotated[ReportSensitivity | None, Query()] = None,
    created_after: Annotated[UtcDateTime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReportListResponse:
    """Return one page of reports the current user may read."""
    page_result = queries.list_reports(
        session,
        access_filter=readable_reports_filter(user, get_role_codes(user)),
        topic_id=topic_id,
        status=status_filter,
        visibility=visibility,
        sensitivity=sensitivity,
        generated_after=created_after,
        page=page,
        page_size=page_size,
    )
    names = _topic_names(session, {item.topic_id for item in page_result.items})
    return ReportListResponse(
        items=[_summary(item, topic_name=names.get(item.topic_id)) for item in page_result.items],
        page=page_result.page,
        page_size=page_result.page_size,
        total=page_result.total,
    )


@router.post("/publish-due", response_model=PublishDueResponse)
def release_due_reports(
    user: WriteUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=PUBLISH_BATCH_LIMIT)] = PUBLISH_BATCH_LIMIT,
) -> PublishDueResponse:
    """Run the release driver once, for operators who do not want to wait for the scheduler.

    This is the same `publish_due_reports` sweep the Celery beat task runs, exposed so the
    admin UI can complete a publishing flow without waiting for the next tick. It is declared
    before the `/{report_id}` routes so the literal path is not read as an id.
    """
    released = publish_due_reports(session, limit=limit)
    blocked = [entry for entry in released if entry["status"] == "failed"]
    return PublishDueResponse(
        released=released,
        released_count=len(released) - len(blocked),
        blocked_count=len(blocked),
    )


@router.get("/{report_id}", response_model=ReportDetailResponse)
def get_report(
    report_id: int,
    request: Request,
    user: ReadUser,
    session: DbSession,
) -> ReportDetailResponse:
    """Return one report with its versions, citations, verdict, and reviews."""
    report = authorize_report_read(session, request, user, report_id=report_id)
    detail = queries.get_report_detail(session, report_id=report.id)
    assert detail is not None
    names = _topic_names(session, {report.topic_id})
    return ReportDetailResponse(
        id=report.id,
        agent_run_id=report.agent_run_id,
        topic_id=report.topic_id,
        topic_name=names.get(report.topic_id),
        owner_id=report.owner_id,
        title=report.title,
        content_markdown=report.content_markdown,
        current_version=report.current_version,
        status=report.status,
        visibility=report.visibility,
        sensitivity=report.sensitivity,
        publish_mode=report.publish_mode,
        publish_at=_iso(report.publish_at),
        generated_at=report.generated_at.isoformat(),
        published_at=_iso(report.published_at),
        versions=[
            ReportVersionResponse(
                id=row.id,
                version=row.version,
                change_reason=row.change_reason,
                created_by=row.created_by,
                created_at=row.created_at.isoformat(),
            )
            for row in detail.versions
        ],
        sources=[
            ReportSourceResponse(
                id=row.id,
                version=row.version,
                section_key=row.section_key,
                source_type=row.source_type,
                source_ref=row.source_ref,
                source_url=row.source_url,
                repository_id=row.repository_id,
                metric_snapshot_id=row.metric_snapshot_id,
            )
            for row in detail.sources
        ],
        verification=None
        if detail.verification is None
        else VerificationResponse(
            version=detail.verification.version,
            verdict=detail.verification.verdict,
            structure_ok=detail.verification.structure_ok,
            citations_ok=detail.verification.citations_ok,
            numbers_ok=detail.verification.numbers_ok,
            sensitivity_ok=detail.verification.sensitivity_ok,
            reason_codes=list(detail.verification.reason_codes_json or []),
            details=detail.verification.details_json,
            created_at=detail.verification.created_at.isoformat(),
        ),
        reviews=[
            ReportReviewResponse(
                id=row.id,
                version=row.version,
                reviewer_id=row.reviewer_id,
                decision=row.decision,
                comment=row.comment,
                created_at=row.created_at.isoformat(),
            )
            for row in detail.reviews
        ],
    )


@router.patch("/{report_id}", response_model=RevisionResponse)
def edit_report(
    report_id: int,
    payload: ReportPatchRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> RevisionResponse:
    """Apply a manual edit, appending an immutable version when the content changes."""
    authorize_report_read(session, request, user, report_id=report_id)
    supplied = payload.model_dump(exclude_unset=True)
    outcome = revise_report(
        session,
        report_id=report_id,
        actor_id=user.id,
        title=payload.title if "title" in supplied else None,
        content_markdown=payload.content_markdown if "content_markdown" in supplied else None,
        visibility=payload.visibility if "visibility" in supplied else None,
        sensitivity=payload.sensitivity if "sensitivity" in supplied else None,
        **({"publish_at": payload.publish_at} if "publish_at" in supplied else {}),
    )
    return RevisionResponse(**outcome.to_dict())


@router.post("/{report_id}/verify", response_model=VerificationOutcomeResponse)
def verify_one(
    report_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> VerificationOutcomeResponse:
    """Run the four `A-004` checks against the current version and store the verdict."""
    authorize_report_read(session, request, user, report_id=report_id)
    return VerificationOutcomeResponse(**verify_report(session, report_id=report_id).to_dict())


@router.post("/{report_id}/review", response_model=ReviewResponse)
def review_one(
    report_id: int,
    payload: ReviewRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> ReviewResponse:
    """Record a human decision on a report awaiting review."""
    authorize_report_read(session, request, user, report_id=report_id)
    outcome = record_review(
        session,
        report_id=report_id,
        decision=payload.decision,
        reviewer_id=user.id,
        comment=payload.comment,
        edited_version_id=payload.edited_version_id,
    )
    return ReviewResponse(**outcome.to_dict())


@router.post("/{report_id}/publish", response_model=PublishResponse)
def publish_one(
    report_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> PublishResponse:
    """Verify, apply the publish policy, and release the report when it is due.

    The policy decision always comes back in `publish_decision`. When the decision queues the
    report and its withdrawal window has already elapsed, the release is attempted here too,
    so the caller learns the outcome of the `INV-5` gate (`409 PUBLISH_POLICY_BLOCKED`) or of
    an illegal state (`409 REPORT_STATE_INVALID`) instead of a bare "queued". A report still
    inside its window is returned as queued, and the scheduled sweep releases it later.
    """
    authorize_report_read(session, request, user, report_id=report_id)
    outcome = publish_or_request_review(session, report_id=report_id)
    status_value = outcome.status
    if (
        outcome.status == "scheduled"
        and outcome.publish_at is not None
        and outcome.publish_at <= utc_now()
    ):
        status_value = publish_report(session, report_id=report_id).status
    return PublishResponse(
        report_id=outcome.report_id,
        status=status_value,
        pipeline_run_id=outcome.pipeline_run_id,
        publish_decision=PublishDecisionResponse(
            report_id=outcome.report_id,
            mode=outcome.mode,
            resolution="pending_review" if outcome.requires_review else "scheduled",
            reason_codes=list(outcome.reason_codes),
            requires_review=outcome.requires_review,
            publish_at=_iso(outcome.publish_at),
        ),
        verification=VerificationOutcomeResponse(**outcome.verification.to_dict()),
    )


@router.post("/{report_id}/rollback", response_model=RevisionResponse)
def rollback_one(
    report_id: int,
    payload: RollbackRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> RevisionResponse:
    """Restore a historical version as a new draft version for re-verification."""
    authorize_report_read(session, request, user, report_id=report_id)
    outcome = rollback_report(
        session,
        report_id=report_id,
        version=payload.version,
        actor_id=user.id,
        comment=payload.comment,
    )
    return RevisionResponse(**outcome.to_dict())


@router.post("/{report_id}/push", response_model=PushOutcomeResponse)
def push_one(
    report_id: int,
    payload: PushRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> PushOutcomeResponse:
    """Deliver one report version to one allowlisted destination.

    Only an `approved` or `published` report may be pushed, and only to a target that is
    enabled and cleared for the report's visibility and sensitivity; every refusal raises and
    is answered as Problem Details.

    A delivery the transport refused is a recorded, retryable result rather than a request
    error, so it comes back as `200` with `status="failed"` and a stable `error_code` — the
    same shape `POST /reports/publish-due` uses for a blocked release. Re-posting the same
    report, version, and target replays the stored result with `replayed=true` instead of
    sending twice.
    """
    authorize_report_read(session, request, user, report_id=report_id)
    outcome = push_report(
        session,
        report_id=report_id,
        channel=payload.channel,
        target_id=payload.target_id,
        actor_id=user.id,
    )
    return PushOutcomeResponse(**outcome.to_dict())
