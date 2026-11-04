"""报告端点：列表、读取、编辑、校验、评审、发布与回滚。

每条路由都委托给 `reports` 服务与授权服务；路由本身只整形请求与响应。
列表的权限过滤在数据库里通过 `readable_reports_filter` 完成，这是
`docs/api-contract.md` §6 的要求：一份无权查看的报告绝不能影响分页内容，
也不能影响返回的总数。
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
from open_source_intelligence.reports.sharing import (
    DEFAULT_SHARE_TTL_HOURS,
    MAX_SHARE_TTL_HOURS,
    create_share_link,
    list_share_links,
    revoke_share_link,
)
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
    """按报告服务其余部分的习惯渲染时间戳。"""
    return value.isoformat() if value else None


class ReportSummaryResponse(BaseModel):
    """列表响应中的一行报告。"""

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
    """分页的报告列表。"""

    items: list[ReportSummaryResponse]
    page: int
    page_size: int
    total: int


class ReportVersionResponse(BaseModel):
    """一个不可变的报告版本。"""

    id: int
    version: int
    change_reason: str | None
    created_by: int | None
    created_at: str


class ReportSourceResponse(BaseModel):
    """当前版本的一条可溯源引用。"""

    id: int
    version: int
    section_key: str
    source_type: str
    source_ref: str | None
    source_url: str | None
    repository_id: int | None
    metric_snapshot_id: int | None


class ReportReviewResponse(BaseModel):
    """一次人工评审决定。"""

    id: int
    version: int
    reviewer_id: int | None
    decision: str
    comment: str | None
    created_at: str


class VerificationResponse(BaseModel):
    """为当前版本存下的 Verifier 结论，前提是它已被检查过。"""

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
    """报告完整详情：版本、引用、结论与评审。"""

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
    """可编辑的报告字段。未提交的字段保持原样。"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_markdown: str | None = Field(default=None, min_length=1)
    visibility: ReportVisibility | None = None
    sensitivity: ReportSensitivity | None = None
    publish_at: datetime | None = None


class ReviewRequest(BaseModel):
    """针对一份待评审报告提交的一次人工评审决定。"""

    decision: ReviewDecision
    comment: str | None = Field(default=None, max_length=2000)
    edited_version_id: int | None = None


class RollbackRequest(BaseModel):
    """把某个历史版本恢复成一个新草稿版本的请求。"""

    version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class PushRequest(BaseModel):
    """把一个报告版本投递到一个允许清单内目标的请求。"""

    channel: Literal["email", "webhook"]
    target_id: int = Field(ge=1)


class RevisionResponse(BaseModel):
    """一次编辑或回滚的结果。"""

    report_id: int
    version: int
    status: str
    content_changed: bool
    changed_fields: list[str]
    restored_from: int | None


class ReviewResponse(BaseModel):
    """记录一次评审决定的结果。"""

    report_id: int
    review_id: int
    decision: str
    status: str


class VerificationOutcomeResponse(BaseModel):
    """运行 Verifier 的结果。"""

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
    """发布端点返回的策略裁决。"""

    report_id: int
    mode: str
    resolution: str
    reason_codes: list[str]
    requires_review: bool
    publish_at: str | None


class PublishResponse(BaseModel):
    """对一份报告施加发布策略后的结果。"""

    report_id: int
    status: str
    pipeline_run_id: int | None
    publish_decision: PublishDecisionResponse
    verification: VerificationOutcomeResponse


class PublishDueResponse(BaseModel):
    """一轮发布扫描的结果。"""

    released: list[dict[str, Any]]
    released_count: int
    blocked_count: int


def _topic_names(session: Session, topic_ids: set[int]) -> dict[int, str]:
    """用一次查询取回指定主题的名字。"""
    if not topic_ids:
        return {}
    rows = session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _summary(report: Any, *, topic_name: str | None) -> ReportSummaryResponse:
    """构造一行列表记录。"""
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
    """返回一页当前用户有权阅读的报告。"""
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
    """手动跑一次发布驱动，供不想等调度器的运维使用。

    这就是 Celery beat 任务跑的那次 `publish_due_reports` 扫描，暴露出来是
    为了让管理界面不必等下一个周期就能走完一条发布流程。它声明在
    `/{report_id}` 路由之前，免得这条字面量路径被当成一个 id 解析。

    扫描范围限定在调用方可读的报告内。没有这个范围，「把到期的都发出去」
    就会让一个编辑发布一份他自己都打不开的 PRIVATE 报告。
    """
    released = publish_due_reports(
        session,
        limit=limit,
        access_filter=readable_reports_filter(user, get_role_codes(user)),
    )
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
    """返回一份报告，连同它的版本、引用、结论与评审。"""
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
    """应用一次人工编辑；正文发生变化时追加一个不可变版本。"""
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
    """对当前版本跑 `A-004` 的四项检查，并保存结论。"""
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
    """记录对一份待评审报告做出的人工决定。"""
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
    """校验、施加发布策略，到点就真正发布这份报告。

    策略裁决总会通过 `publish_decision` 返回。当裁决把报告排入队列、而它的
    撤回窗口已经过去时，这里也会顺带尝试发布，好让调用方直接拿到 `INV-5`
    闸门的结果（`409 PUBLISH_POLICY_BLOCKED`）或非法状态的结果
    （`409 REPORT_STATE_INVALID`），而不是一句干巴巴的「已入队」。仍在
    窗口内的报告按已入队返回，由定时扫描稍后发布。
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
    """把某个历史版本恢复成一个新草稿版本，等待重新校验。"""
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
    """把一个报告版本投递到一个允许清单内的目标。

    只有 `approved` 或 `published` 的报告可以推送，且只能推给一个已启用、
    并对该报告的可见性与敏感度都放行的目标；每一次拒绝都抛异常，并以
    Problem Details 作答。

    传输层拒绝的投递是一个已记录、可重试的结果，而不是请求错误，所以它回
    `200`，带 `status="failed"` 和一个稳定的 `error_code` —— 与
    `POST /reports/publish-due` 对被拦截发布使用的形状一致。重复提交同一份
    报告、同一版本、同一目标时，会重放已存的结果并返回 `replayed=true`，
    而不是发两次。
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


class ShareLinkRequest(BaseModel):
    """新分享链接的有效期。留空表示链接永不过期。"""

    ttl_hours: int | None = Field(default=DEFAULT_SHARE_TTL_HOURS, ge=1, le=MAX_SHARE_TTL_HOURS)


class ShareLinkResponse(BaseModel):
    """唯一一个携带明文令牌的响应。"""

    share_id: int
    token: str
    url_path: str
    expires_at: datetime | None


class ShareLinkSummaryResponse(BaseModel):
    """之后可被列出的链接形态：不含令牌，只有状态与用量。"""

    share_id: int
    created_by: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_accessed_at: datetime | None
    access_count: int
    created_at: datetime
    revoked: bool
    active: bool


class ShareLinkListResponse(BaseModel):
    """为某一份报告签发过的全部链接。"""

    items: list[ShareLinkSummaryResponse]


class ShareRevokeResponse(BaseModel):
    """本次调用是否真的执行了撤销。`false` 表示此前已被撤销。"""

    share_id: int
    revoked: bool


@router.post("/{report_id}/shares", response_model=ShareLinkResponse, status_code=201)
def create_report_share(
    report_id: int,
    payload: ShareLinkRequest,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> ShareLinkResponse:
    """为一份已发布的 `PUBLIC` 报告签一条免登录链接。

    闸门看的是 `sensitivity`，不是 `visibility`。分享链接是一个背后没有
    登录的地址，所以它问的是「这份内容可以离开组织吗」—— 那是
    `sensitivity` 回答的问题。若改用 `visibility` 把关，一份 `INTERNAL`
    报告（每个登录用户都能读）就会从一扇公开的门里走出去。

    `token` 只出现在这个响应里，别处没有：库里只存它的 sha256，因此链接
    事后无法找回，一次数据库导出也不等于一组可用的凭据。
    """
    authorize_report_read(session, request, user, report_id=report_id)
    created = create_share_link(
        session,
        report_id=report_id,
        actor_id=user.id,
        ttl_hours=payload.ttl_hours,
    )
    return ShareLinkResponse(**created.to_dict())


@router.get("/{report_id}/shares", response_model=ShareLinkListResponse)
def list_report_shares(
    report_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> ShareLinkListResponse:
    """列出为某份报告签发过的链接。永远不返回明文令牌。"""
    authorize_report_read(session, request, user, report_id=report_id)
    return ShareLinkListResponse(
        items=[
            ShareLinkSummaryResponse(**summary.to_dict())
            for summary in list_share_links(session, report_id=report_id)
        ]
    )


@router.delete("/{report_id}/shares/{share_id}", response_model=ShareRevokeResponse)
def revoke_report_share(
    report_id: int,
    share_id: int,
    request: Request,
    user: WriteUser,
    session: DbSession,
) -> ShareRevokeResponse:
    """让一条链接不再能解析。

    幂等：第二次调用回 `revoked=false` 而不是报错，因为调用方的意图 ——「确保这个地址失效」——
    已经满足了。
    """
    authorize_report_read(session, request, user, report_id=report_id)
    revoked = revoke_share_link(
        session,
        report_id=report_id,
        share_id=share_id,
        actor_id=user.id,
    )
    return ShareRevokeResponse(share_id=share_id, revoked=revoked)
