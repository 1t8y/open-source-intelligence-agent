"""按风险分级执行发布：验证、决策，并驱动报告状态机。

`ADR-003` 把发布拆成三种方式：`AUTO` 立即发布干净、低敏的报告，`AUTO_WITH_DELAY` 先
搁置一段撤稿窗口，`MANUAL` 则等人。拆分的用意是：常规周报永远不会被某个人卡住，而真正
异常的报告则一定卡在人的环节上；因此下面的策略只有在能说出具体理由时，才升级到
`MANUAL`。

状态机就是 `INTERFACES.md` §8 声明的那一台：

```text
draft -> pending_review -> approved -> scheduled -> published
```

其中 `failed` 是终止性的异常分支，`changes_requested` 是回退分支。`publish_due_reports`
是唯一的驱动器：它把 `approved` 报告推上发布路径，并在撤稿窗口过去之后放行 `scheduled`
报告。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.notifications.service import push_after_publish
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportVerification,
    ReportVersion,
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
    ReportNotFoundError,
    ReportPublishError,
    ReportStateError,
)
from .state import transition_report
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

#: `publish_due_reports` 单趟放行报告数量的上界。
PUBLISH_BATCH_LIMIT: Final[int] = 100


@dataclass(frozen=True, slots=True)
class PublishDecision:
    """对一份报告的策略裁决，形态与 API 的 `publish_decision` 对象一致。"""

    report_id: int
    mode: str
    resolution: str
    reason_codes: tuple[str, ...]
    requires_review: bool
    publish_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的载荷。"""
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
    """把发布策略套用到一份报告后的结果。"""

    report_id: int
    status: str
    mode: str
    reason_codes: tuple[str, ...]
    requires_review: bool
    publish_at: datetime | None
    pipeline_run_id: int | None
    verification: VerificationOutcome

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的载荷。"""
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
    """记录一次人工评审决定的结果。"""

    report_id: int
    review_id: int
    decision: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        """返回可安全转 JSON 的载荷。"""
        return {
            "report_id": self.report_id,
            "review_id": self.review_id,
            "decision": self.decision,
            "status": self.status,
        }


def _run_closed_at(now: datetime | None, moment: datetime) -> datetime | None:
    """把被钉住的时钟回传成运行的结束时间；`None` 表示改用挂钟时间。

    每次发布运行都以调用方的 `moment` 作为开始时间，而 `pipeline_runs` 要求
    `finished_at >= started_at`。`moment` 已被钉住却用挂钟时间收尾，记录就会自相矛盾，
    所以这里把被钉住的 `now` 一路带下去。
    """
    return moment if now is not None else None


def _config_fingerprint(session: Session, agent_run: AgentRun) -> tuple[str, str, str]:
    """返回一次运行执行时所用的模型、Prompt 版本与工具策略。"""
    config = session.get(AgentConfig, agent_run.agent_config_id)
    if config is None:
        return ("", "", "")
    return (
        config.model,
        config.prompt_version,
        json.dumps(config.tool_policy_json, sort_keys=True),
    )


def _latest_published_report(session: Session, *, topic_id: int, exclude_id: int) -> Report | None:
    """返回同一主题下最近发布过的另一份报告；若没有则返回 `None`。"""
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


def _baseline_reasons(
    session: Session,
    report: Report,
    *,
    skip_first_run: bool = False,
) -> list[str]:
    """收集来自运行与主题历史、而非来自产物本身的理由。

    `skip_first_run` 由 `AUTO_WITH_DELAY` 置位：该模式是无人值守的自动发布路径，本身
    就带一道验证关卡加一个撤稿窗口，若再把主题的首份报告压着等人，每个本该独立运行的
    新主题都会被拖住。`MANUAL` 与 `AUTO` 保留首轮评审，因为人的环节本来就在它们的
    链路里。
    """
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
        if not skip_first_run:
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
    """判定一份报告是立即发布、延迟发布，还是等评审。

    配置的 `publish_mode` 只设定上限；具体发现只能让决定更严，绝不会更松。
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
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
    reasons.extend(
        _baseline_reasons(
            session,
            report,
            skip_first_run=report.publish_mode == PUBLISH_MODE_AUTO_WITH_DELAY,
        )
    )
    if report.publish_mode == PUBLISH_MODE_AUTO and report.visibility != INTERNAL_VISIBILITY:
        # `AUTO` 只留给内部报告；更窄的可见性一律走评审。
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
    """把一份已批准的报告放进发布队列。"""
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
    transition_report(report, "scheduled")
    report.publish_at = moment
    session.commit()
    return report


def withdraw_report(
    session: Session,
    *,
    report_id: int,
    actor_id: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> Report:
    """把一份已发布的报告拉回编辑队列。

    `published -> draft` 是 `published` 唯一的出边（见 `state.py`），撤稿正是它存在的
    理由：报告不再公开，丢掉它原本占着的发布窗口，并且必须重新通过验证和策略才能再发
    出去。它当初以哪个版本发布，仍然留在历史里 —— 撤稿不是删除。

    这里不需要清理分享链接：分享闸门是 `sensitivity=PUBLIC` **且**
    `status=published`，所以状态一动，已撤稿报告的链接就立刻解析不到。
    """
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
    if report.status != "published":
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a published report can be withdrawn"
        )
    published_at = report.published_at
    transition_report(report, "draft")
    report.publish_at = None
    report.published_at = None
    record_audit_event(
        session,
        action="report.withdraw",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        actor_id=actor_id,
        metadata_json={
            "version": report.current_version,
            "published_at": published_at.isoformat() if published_at is not None else None,
            "withdrawn_at": moment.isoformat(),
            "comment": comment,
        },
    )
    session.commit()
    return report


def publish_report(
    session: Session,
    *,
    report_id: int,
    now: datetime | None = None,
) -> Report:
    """发布一份已排期的报告，先断言 `INV-5` 的前置条件。

    撤稿窗口在这里校验而不是在调用方，任何代码路径都无法提前放行一份报告。`MANUAL`
    报告还必须带有一次针对当前发布版本的 `approve` 评审。
    """
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
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
    transition_report(report, "published")
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
    # 发布才是订阅者关心的事件，而所有发布路径都汇聚到这里，所以自动推送只接一次。
    # 它从不抛异常：目标不可用或传输被拒会记在推送行上，由重试清扫接手 —— 一次通知
    # 失败不能把已经发生的发布撤回去。
    push_after_publish(session, report_id=report.id, now=now)
    return report


def publish_or_request_review(
    session: Session,
    *,
    report_id: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PublishOutcome:
    """验证报告、套用发布策略，并记下决定。

    这里写入的 `publish` 运行跟踪的是*决定*，不是最终的发布：只要策略被干净地套用就算
    成功，即使结果是 `pending_review` —— 请求人工介入是正常结果，不是错误。

    已经越过决策点的报告不会被悄悄推进，只有一个刻意保留的例外：`approved` 报告会被
    直接排进立即放行，因为人工批准就是明确同意，跳过撤稿窗口是文档写明的后果。
    """
    resolved_settings = settings or get_settings()
    moment = now or utc_now()
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
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

    if report.status == "pending_review":
        # 只有人工评审才能清掉这个状态；重新评估不得推进它。
        pass
    elif report.status == "published":
        # 已经发出去了，没有可决定的东西。
        pass
    elif report.status == "scheduled":
        # 已经排好队；`publish_at` 归放行驱动器管。
        pass
    elif report.status == "approved":
        # 批准就是明确同意，所以立即入队并跳过撤稿窗口 —— 与 `publish_due_reports`
        # 处理 approved 报告时做的动作一致。
        transition_report(report, "scheduled")
        report.publish_at = moment
    elif decision.requires_review:
        transition_report(report, "pending_review")
        report.publish_at = None
    else:
        transition_report(report, "scheduled")
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
    """记录一次针对待评审报告的人工决定。

    `approve` 放行发布，`request_changes` 打回重写，`reject` 以 `failed` 终结它，使其
    永远无法发布。
    """
    moment = now or utc_now()
    if decision not in {REVIEW_APPROVE, REVIEW_REJECT, REVIEW_REQUEST_CHANGES}:
        raise ReportGenerationError(f"unsupported review decision: {decision}")
    report = session.get(Report, report_id)
    if report is None:
        raise ReportNotFoundError(f"report {report_id} does not exist")
    if report.status != "pending_review":
        raise ReportStateError(
            f"report {report.id} is {report.status}; only a report pending review can be reviewed"
        )
    target = {
        REVIEW_APPROVE: "approved",
        REVIEW_REJECT: "failed",
        REVIEW_REQUEST_CHANGES: "changes_requested",
    }[decision]
    if edited_version_id is not None:
        # 这一列是外键，但外键只证明该行存在：它照样会记下一次针对*另一份*报告某个版本
        # 的评审，而一个不存在的 id 会表现为数据库错误，而不是被拒绝的请求。在这里把
        # 这一对解析清楚可以同时覆盖两种情况，并且在迁移之前做，被拒绝的评审就不会留下
        # 一个迁到一半的状态。
        owned = session.scalar(
            select(ReportVersion.id).where(
                ReportVersion.id == edited_version_id,
                ReportVersion.report_id == report.id,
            )
        )
        if owned is None:
            raise ReportGenerationError(
                f"report {report.id} has no version row {edited_version_id}"
            )
    transition_report(report, target)
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
    """记录一次被拦下的发布，并让这份报告不再在每轮清扫中重试。

    把被策略拦下的报告留在 `scheduled`，会让它无限重试且始终不可见；`failed` 是终态，
    于是它会凸显为一件需要人处理的工作。
    """
    pipeline_run = start_pipeline_run(
        session,
        run_type="publish",
        topic_id=report.topic_id,
        attempt=1,
    )
    mark_pipeline_run_running(session, pipeline_run, started_at=moment)
    transition_report(report, "failed")
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
    access_filter: ColumnElement[bool] | None = None,
) -> list[dict[str, Any]]:
    """放行所有到期的报告，顺带把人工批准的报告推到发布路径上。

    批准就是明确同意，所以 `approved` 报告跳过撤稿窗口、直接排队等待立即放行；
    `scheduled` 报告则只有等到 `publish_at` 过去之后才放行。一份被拦下的报告绝不会
    中断整轮清扫：它会被标成 `failed` 并带上稳定的错误码，清扫继续 —— 与「单个主题
    失败不会拖垮每日流水线」是同一个道理。

    `access_filter` 把清扫限制在触发者有权阅读的报告范围内。手动接口会传它，因为
    「放行所有到期项」是对调用方看得见的那些报告的权限，而不是对整个数据库的权限；
    定时 beat 任务传 `None`，因为它代表系统行事，而不是代表某一个人。
    """
    moment = now or utc_now()
    if limit < 1:
        raise ValueError("limit must be at least 1")
    approved_statement = select(Report).where(Report.status == "approved")
    if access_filter is not None:
        approved_statement = approved_statement.where(access_filter)
    approved = session.scalars(approved_statement.order_by(Report.id).limit(limit)).all()
    for report in approved:
        schedule_report(session, report_id=report.id, now=moment)

    due_statement = select(Report).where(Report.status == "scheduled", Report.publish_at <= moment)
    if access_filter is not None:
        due_statement = due_statement.where(access_filter)
    due = session.scalars(due_statement.order_by(Report.publish_at, Report.id).limit(limit)).all()
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
