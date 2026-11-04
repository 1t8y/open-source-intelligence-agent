"""报告推送服务：守卫、白名单、投递、记录、重试。

服务是报告离开系统的唯一出口，因此下面的顺序就是全部的安全故事：

1. 对报告状态做**守卫**。只有 `approved` 或 `published` 的报告才能推送，这就是「只有已
   批准的报告才能推送」在实践中的含义 —— 仅仅生成出来的草稿没有理由被寄往任何地方。
2. 对受众做**守卫**。`CONFIDENTIAL` 报告还需要针对所发版本的人工 `approve` 评审，因为
   `REQUIREMENTS.md` §7 把敏感材料的外部投递保留给 `MANUAL`。
3. 对照 `push_targets` **解析**目的地。白名单是抵达真实地址的唯一途径，并且目标必须已
   启用，且针对报告的可见范围与敏感级别被明确放行。
4. 通过渠道适配器**投递**，然后**记录**这次尝试。

投递失败是记录下来的结果，而不是异常：`report_pushes` 行保留错误码与尝试次数，因此
`retry_failed_pushes` 能把它捡起来；而针对同一 (report, version, target) 的重复请求会重放
已存结果，而不是再发一次。
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

#: 为一次推送尝试记录的运行类型；与其他主题范围内的运行一并注册。
PUSH_RUN_TYPE: Final[str] = "push"
#: 单次重试清扫最多尝试的推送数上限。
PUSH_BATCH_LIMIT: Final[int] = 100
#: 报告可以推送的唯一状态集合。
PUSHABLE_STATUSES: Final[frozenset[str]] = frozenset({"approved", "published"})

CONFIDENTIAL_SENSITIVITY: Final[str] = "CONFIDENTIAL"
PRIVATE_VISIBILITY: Final[str] = "PRIVATE"
REVIEW_APPROVE: Final[str] = "approve"

#: 持久化到推送行上的最长传输细节。
MAX_DETAIL_LENGTH: Final[int] = 512
#: 传输层拒收或投递失败时记录的错误码。
PUSH_ERROR_CODE: Final[str] = "PUSH_DELIVERY_FAILED"
#: 重试指向报告已越过的版本时记录的错误码。
PUSH_VERSION_SUPERSEDED: Final[str] = "PUSH_VERSION_SUPERSEDED"
#: 通知中随附的报告正文长度。
BODY_EXCERPT_CHARS: Final[int] = 800
SUBJECT_PREFIX: Final[str] = "[开源技术情报]"


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """一次投递尝试的结果，无论成功与否。"""

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
        """返回 JSON 安全载荷。"""
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
    """由一条已持久化的推送行构造结果对象。"""
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
    """加载报告，失败时给出稳定错误。"""
    report = session.get(Report, report_id)
    if report is None:
        raise PushNotFound(f"report {report_id} does not exist")
    return report


def _has_approval(session: Session, report: Report) -> bool:
    """返回人工是否批准了当前正在推送的版本。"""
    approval = session.scalar(
        select(ReportReview.id).where(
            ReportReview.report_id == report.id,
            ReportReview.version == report.current_version,
            ReportReview.decision == REVIEW_APPROVE,
        )
    )
    return approval is not None


def build_message(session: Session, report: Report) -> OutboundMessage:
    """渲染目标针对某个报告版本收到的通知。"""
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
    """返回白名单中的目标，拒绝任何未针对本报告放行的条目。"""
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
    """返回白名单，最新的在前，可按条件过滤。"""
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
    """返回投递记录，最新的在前，可按条件过滤。

    `access_filter` 是作为 SQL 谓词传入的报告 ACL。这份账本会点名报告标题、版本、目的地与
    错误文本，所以仅凭角色检查并不够：必须像报告列表那样在查询里过滤，否则调用方就能读到
    它无权打开的报告的元数据。`None` 表示「系统范围」，只在后台清扫中才是正确的。
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
    """把某一个报告版本投递给某一个白名单目标。

    守卫违规直接抛出；传输失败则被记录，并以 `status="failed"` 返回。对已经成功的投递再次
    发起请求会原样返回已存结果，这正是整个操作按 (report, version, target) 幂等的原因。
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

    # 在写入任何内容之前构造：未配置的实时渠道是配置错误，而不是投递失败，且绝不能留下
    # 一条推送行。
    adapter = notifier or build_channel(resolved, channel)
    message = build_message(session, report)

    row = existing or ReportPush(
        report_id=report.id,
        target_id=target.id,
        version=target_version,
        channel=target.channel,
        destination=target.destination,
        target_name=target.name,
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
        row.target_name = target.name
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
    """通过适配器发送，把任何意外失败转成回执。

    推送处在发布路径内部，所以适配器的缺陷绝不能把一次成功发布变成崩溃的运行；它只会变成
    一条已记录、可重试的失败。
    """
    try:
        return adapter.send(destination=destination, message=message)
    except Exception as exc:  # 坏掉的适配器绝不能导致推送记录丢失。
        return DeliveryReceipt(
            ok=False,
            detail=f"adapter:{exc.__class__.__name__}:{exc}"[:MAX_DETAIL_LENGTH],
        )


def _closed_at(now: datetime | None, moment: datetime) -> datetime | None:
    """把被钉住的时钟回显为运行的关闭时间，与发布的约定保持一致。"""
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
    """把刚发布的报告自动推送到每一个已启用的目标。

    这段逻辑运行在发布路径内部，因此它从不抛错：不可用的目标或被拒的传输会被记录（或被
    跳过），发布本身照常成立。推送失败是重试候选，绝不是撤回报告的理由。
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
        except Exception as exc:  # 自动推送绝不能破坏发布，无论哪一步失败。
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
    """按一条推送记录的 (报告, 版本, 目标) 三元组重跑，以重试它。"""
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
    """重试每一条仍有重试预算的失败推送。

    单条不可用的行绝不会让清扫停下：与 `publish_due_reports` 一致，失败的重试会被跳过，
    批次中其余部分继续。

    `access_filter` 把清扫收窄到调用方有权读取的报告。操作员按下「重试」时是在处理自己的
    报告，而不是悄悄重发它打不开的材料；定时清扫则留空为 `None`，因为 beat 任务不属于任何
    单个用户。
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
            # 一行可能不再可重试（报告离开了可推送状态，或目标被禁用）。
            # 记录原因，而不是静默丢弃它。
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
        except Exception:  # 一条坏行绝不能中止整次清扫。
            session.rollback()
    return outcomes
