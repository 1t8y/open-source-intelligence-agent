"""守护基于风险的发布策略与报告状态机。

`INTERFACES.md` §8 声明的状态机就是被测的契约：

```text
draft -> pending_review -> approved -> scheduled -> published
```

其中 `failed` 为终态，`changes_requested` 是回退分支。策略只能收紧配置好的
`publish_mode`，绝不能放松它。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from reports_support import (
    AUTO,
    AUTO_WITH_DELAY,
    MANUAL,
    SeededReport,
    build_report,
    seed_published_sibling,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
    ReportVersion,
)
from open_source_intelligence.pipeline.runs import list_pipeline_runs
from open_source_intelligence.reports.errors import (
    PublishPolicyBlocked,
    ReportGenerationError,
    ReportStateError,
)
from open_source_intelligence.reports.publishing import (
    PUBLISH_MODE_AUTO,
    PUBLISH_MODE_AUTO_WITH_DELAY,
    PUBLISH_MODE_MANUAL,
    REASON_FIRST_RUN,
    REASON_NOT_INTERNAL_VISIBILITY,
    REASON_SENSITIVE_REPORT,
    REASON_VERIFICATION_FAILED,
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_REQUEST_CHANGES,
    evaluate_publish_policy,
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
    schedule_report,
    withdraw_report,
)
from open_source_intelligence.reports.verification import (
    REASON_LOW_CITATION_COVERAGE,
    REASON_SENSITIVE_CONTENT,
    verify_report,
)

SUMMARY_KEY = "summary"
APPENDIX_KEY = "appendix"


def _sources(session: Session, report_id: int, section_key: str) -> list[ReportSource]:
    return list(
        session.scalars(
            select(ReportSource).where(
                ReportSource.report_id == report_id,
                ReportSource.section_key == section_key,
            )
        ).all()
    )


def _force_scheduled(
    session: Session,
    case: SeededReport,
    *,
    publish_at=None,
    verify: bool = True,
) -> Report:
    """把报告摆到 `scheduled`，并（可选地）先跑一次通过的校验。"""
    if verify:
        verify_report(session, report_id=case.report_id, now=NOW)
    report = case.reload(session)
    report.status = "scheduled"
    report.publish_at = NOW if publish_at is None else publish_at
    session.commit()
    return report


def _prepare_case(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str,
    prior_publication: bool = True,
    repo_count: int = 2,
) -> SeededReport:
    """建一份报告，并默认给它的主题补一份更早的已发布报告。"""
    case = build_report(session, workspace, publish_mode=publish_mode, repo_count=repo_count)
    if prior_publication:
        seed_published_sibling(session, case)
    return case


# --------------------------------------------------------------------------------------
# 策略评估
# --------------------------------------------------------------------------------------


def test_auto_with_delay_schedules_a_clean_report_after_the_window(
    session: Session,
    report_case: SeededReport,
) -> None:
    """主题不是首轮时，常规的内部报告应当入队，而不是被挡住。"""
    seed_published_sibling(session, report_case)

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_AUTO_WITH_DELAY
    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert decision.reason_codes == ()
    assert decision.publish_at == NOW + timedelta(minutes=60)


def test_delay_minutes_come_from_settings(
    session: Session,
    report_case: SeededReport,
) -> None:
    """撤回窗口由运维通过配置控制。"""
    seed_published_sibling(session, report_case)

    decision = evaluate_publish_policy(
        session,
        report_id=report_case.report_id,
        settings=build_settings(publish_delay_minutes=15),
        now=NOW,
    )

    assert decision.publish_at == NOW + timedelta(minutes=15)


def test_first_run_asks_for_review_on_manual(
    session: Session,
    workspace: Workspace,
) -> None:
    """`MANUAL` 报告的首轮没有基线，必须由人签字。"""
    case = _prepare_case(session, workspace, publish_mode=MANUAL, prior_publication=False)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert decision.publish_at is None
    assert REASON_FIRST_RUN in decision.reason_codes


def test_auto_with_delay_skips_first_run_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """无人值守的自动发布路径，不会把主题的第一份报告卡住。

    `AUTO_WITH_DELAY` 本身已经带着校验闸门与撤回窗口，若首份报告还要人签字，
    每一个新接入自动发布的主题都会被拖住。首轮必须入队而不是阻塞，也不该带
    `FIRST_RUN` 这个理由。
    """
    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert REASON_FIRST_RUN not in decision.reason_codes
    assert decision.publish_at == NOW + timedelta(minutes=60)


def test_low_citation_coverage_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """引用被删过的报告，即便在熟主题上也不能自动发布。"""
    seed_published_sibling(session, report_case)
    for source in _sources(session, report_case.report_id, APPENDIX_KEY):
        session.delete(source)
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert REASON_VERIFICATION_FAILED in decision.reason_codes
    assert REASON_LOW_CITATION_COVERAGE in decision.reason_codes
    assert REASON_FIRST_RUN not in decision.reason_codes


def test_sensitive_artifact_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """像凭据的文本会一路传到策略里，变成具体的拦截理由。"""
    seed_published_sibling(session, report_case)
    report = report_case.reload(session)
    report.content_markdown += '\napi_key = "sk-abcdefghijklmnopqrst"\n'
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert REASON_SENSITIVE_CONTENT in decision.reason_codes


def test_confidential_sensitivity_escalates_to_review(
    session: Session,
    report_case: SeededReport,
) -> None:
    """机密报告永远不会在无人值守的情况下放出去。"""
    seed_published_sibling(session, report_case)
    report = report_case.reload(session)
    report.sensitivity = "CONFIDENTIAL"
    report.visibility = "PRIVATE"
    session.commit()

    decision = evaluate_publish_policy(session, report_id=report_case.report_id, now=NOW)

    assert decision.requires_review is True
    assert REASON_SENSITIVE_REPORT in decision.reason_codes


def test_manual_mode_always_requires_review(
    session: Session,
    workspace: Workspace,
) -> None:
    """`MANUAL` 是天花板：再干净的报告也得等人点头。"""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_MANUAL
    assert decision.requires_review is True
    assert decision.resolution == "pending_review"
    assert decision.reason_codes == ()


def test_auto_requires_internal_visibility(
    session: Session,
    workspace: Workspace,
) -> None:
    """`AUTO` 只留给内部报告；范围收窄就得走复核。"""
    case = _prepare_case(session, workspace, publish_mode=AUTO)

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)
    assert decision.requires_review is True
    assert REASON_NOT_INTERNAL_VISIBILITY in decision.reason_codes

    report = case.reload(session)
    report.visibility = "INTERNAL"
    session.commit()

    relaxed = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)
    assert relaxed.requires_review is False
    assert relaxed.publish_at == NOW


def test_auto_publishes_immediately_when_clean_and_internal(
    session: Session,
    workspace: Workspace,
) -> None:
    """`AUTO` 没有撤回窗口：`publish_at` 就是判定时刻。"""
    case = _prepare_case(session, workspace, publish_mode=AUTO)
    report = case.reload(session)
    report.visibility = "INTERNAL"
    session.commit()

    decision = evaluate_publish_policy(session, report_id=case.report_id, now=NOW)

    assert decision.mode == PUBLISH_MODE_AUTO
    assert decision.resolution == "scheduled"
    assert decision.requires_review is False
    assert decision.publish_at == NOW


def test_evaluating_an_unknown_report_is_a_caller_error(session: Session) -> None:
    """策略评估面对不存在的报告，绝不悄悄成功。"""
    with pytest.raises(ReportGenerationError, match="does not exist"):
        evaluate_publish_policy(session, report_id=987654, now=NOW)


# --------------------------------------------------------------------------------------
# 人工复核
# --------------------------------------------------------------------------------------


def _park_for_review(session: Session, case: SeededReport) -> Report:
    report = case.reload(session)
    assert report.status == "draft"
    report.status = "pending_review"
    session.commit()
    return report


def test_record_review_approve_clears_for_publishing(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`approve` 是明确的同意：报告由此获得入队资格。"""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_APPROVE,
        reviewer_id=1,
        comment="looks good",
        now=NOW,
    )

    assert outcome.decision == REVIEW_APPROVE
    assert outcome.status == "approved"
    assert report_case.reload(session).status == "approved"
    row = session.scalar(select(ReportReview))
    assert row is not None
    assert row.decision == REVIEW_APPROVE
    assert row.version == 1


def test_record_review_request_changes_sends_it_back(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`request_changes` 把报告送回改写路径，而不是入队。"""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_REQUEST_CHANGES,
        reviewer_id=1,
        now=NOW,
    )

    assert outcome.status == "changes_requested"
    with pytest.raises(ReportStateError):
        schedule_report(session, report_id=report_case.report_id, now=NOW)


def test_record_review_reject_terminates_as_failed(
    session: Session,
    report_case: SeededReport,
) -> None:
    """`reject` 是终态，此后这份报告永不可能发布。"""
    _park_for_review(session, report_case)

    outcome = record_review(
        session,
        report_id=report_case.report_id,
        decision=REVIEW_REJECT,
        now=NOW,
    )

    assert outcome.status == "failed"
    with pytest.raises(ReportStateError):
        schedule_report(session, report_id=report_case.report_id, now=NOW)


def test_record_review_requires_pending_state(
    session: Session,
    report_case: SeededReport,
) -> None:
    """只有在报告确实等着复核时，才能记录决定。"""
    report = report_case.reload(session)
    report.status = "scheduled"
    session.commit()

    with pytest.raises(ReportStateError, match="pending review"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            now=NOW,
        )


def test_record_review_rejects_an_unknown_decision(
    session: Session,
    report_case: SeededReport,
) -> None:
    """只接受声明过的三种决定。"""
    _park_for_review(session, report_case)

    with pytest.raises(ReportGenerationError, match="unsupported review decision"):
        record_review(session, report_id=report_case.report_id, decision="rubber_stamp", now=NOW)


def test_record_review_rejects_a_foreign_edited_version(
    session: Session,
    workspace: Workspace,
    report_case: SeededReport,
) -> None:
    """`edited_version_id` 必须指向*这份*报告的版本。

    外键只能证明那一行存在，所以属于另一份报告的版本此前会被悄悄记下。这项检查
    也排在状态迁移之前，被拒绝的复核就不会挪动报告。
    """
    _park_for_review(session, report_case)
    other = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    foreign_version_id = session.scalar(
        select(ReportVersion.id).where(ReportVersion.report_id == other.report_id)
    )
    assert foreign_version_id is not None

    with pytest.raises(ReportGenerationError, match="no version row"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            edited_version_id=foreign_version_id,
            now=NOW,
        )

    assert report_case.reload(session).status == "pending_review"


def test_record_review_rejects_an_unknown_edited_version(
    session: Session,
    report_case: SeededReport,
) -> None:
    """未知 id 是一次被拒的请求，而不是数据库层面的失败。"""
    _park_for_review(session, report_case)

    with pytest.raises(ReportGenerationError, match="no version row"):
        record_review(
            session,
            report_id=report_case.report_id,
            decision=REVIEW_APPROVE,
            edited_version_id=999_999,
            now=NOW,
        )


# --------------------------------------------------------------------------------------
# 发布前置条件（`INV-5`）
# --------------------------------------------------------------------------------------


def test_publish_report_requires_a_passing_verification(
    session: Session,
    report_case: SeededReport,
) -> None:
    """没有判定就发布，正是 `INV-5` 所禁止的。"""
    _force_scheduled(session, report_case, verify=False)

    with pytest.raises(PublishPolicyBlocked, match="no passing verification"):
        publish_report(session, report_id=report_case.report_id, now=NOW)

    assert report_case.reload(session).status == "scheduled"


def test_publish_report_requires_a_failed_verification_to_block(
    session: Session,
    report_case: SeededReport,
) -> None:
    """已记录的*失败*判定，与根本没有判定一样会挡路。"""
    report = report_case.reload(session)
    report.content_markdown = report.content_markdown.replace("## 本周摘要", "## 摘要", 1)
    session.commit()
    _force_scheduled(session, report_case, verify=True)

    with pytest.raises(PublishPolicyBlocked, match="no passing verification"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_manual_publish_requires_an_approval(
    session: Session,
    workspace: Workspace,
) -> None:
    """`MANUAL` 报告多一道闸门：这个版本必须被批准过。"""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)
    _force_scheduled(session, case, verify=True)

    with pytest.raises(PublishPolicyBlocked, match="no approve review"):
        publish_report(session, report_id=case.report_id, now=NOW)

    session.add(
        ReportReview(
            report_id=case.report_id,
            version=1,
            reviewer_id=1,
            decision=REVIEW_APPROVE,
            created_at=NOW,
        )
    )
    session.commit()

    published = publish_report(session, report_id=case.report_id, now=NOW)
    assert published.status == "published"


def test_publish_report_refuses_before_the_window_elapses(
    session: Session,
    report_case: SeededReport,
) -> None:
    """撤回窗口在发布动作内部强制，而不是靠调用方自觉。"""
    _force_scheduled(session, report_case, publish_at=NOW + timedelta(hours=1))

    with pytest.raises(ReportStateError, match="withdrawal window has not elapsed"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_publish_report_requires_scheduled_state(
    session: Session,
    report_case: SeededReport,
) -> None:
    """只有已入队的报告才能被放出去。"""
    with pytest.raises(ReportStateError, match="only a scheduled report"):
        publish_report(session, report_id=report_case.report_id, now=NOW)


def test_publish_report_records_a_run_and_audit(
    session: Session,
    report_case: SeededReport,
) -> None:
    """一次成功的发布要留下一条 `publish` 流水线运行与一条审计事件。"""
    _force_scheduled(session, report_case)

    published = publish_report(session, report_id=report_case.report_id, now=NOW)

    assert published.status == "published"
    assert published.published_at == NOW
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs
    assert runs[0].status == "succeeded"
    assert runs[0].metrics_json is not None
    assert runs[0].metrics_json["stage"] == "publish"
    # 钉住的时钟也必须用于收尾，否则 `finished_at >= started_at` 会不成立。
    assert runs[0].started_at == NOW
    assert runs[0].finished_at == NOW
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "report.publish")).all()
    assert any(event.result == "success" for event in audits)


def test_withdraw_report_returns_a_published_report_to_draft(
    session: Session,
    report_case: SeededReport,
) -> None:
    """撤回是离开 `published` 的唯一一条边，它同时清掉发布窗口。

    在它出现之前，已发布的报告根本没法修正：改写需要草稿态，回滚需要第二个版本，
    于是只有单个版本的已发布报告就被卡死了。
    """
    _force_scheduled(session, report_case)
    publish_report(session, report_id=report_case.report_id, now=NOW)
    published_version = session.get(Report, report_case.report_id).current_version

    withdrawn = withdraw_report(
        session,
        report_id=report_case.report_id,
        actor_id=1,
        comment="数字口径要改",
        now=NOW,
    )

    assert withdrawn.status == "draft"
    assert withdrawn.publish_at is None
    assert withdrawn.published_at is None
    # 撤回既不是删除也不是修订：已发布的版本原样保留。
    assert withdrawn.current_version == published_version
    events = session.scalars(select(AuditLog).where(AuditLog.action == "report.withdraw")).all()
    assert [event.result for event in events] == ["success"]
    assert events[0].metadata_json["version"] == published_version
    assert events[0].metadata_json["comment"] == "数字口径要改"


def test_withdraw_report_refuses_a_report_that_was_never_published(
    session: Session,
    report_case: SeededReport,
) -> None:
    """草稿没有什么可撤回的；拒绝这条边的是状态机，不是调用方。"""
    assert report_case.reload(session).status == "draft"

    with pytest.raises(ReportStateError, match="only a published report"):
        withdraw_report(session, report_id=report_case.report_id, now=NOW)

    assert session.get(Report, report_case.report_id).status == "draft"


# --------------------------------------------------------------------------------------
# 应用策略
# --------------------------------------------------------------------------------------


def test_publish_or_request_review_queues_a_clean_delayed_report(
    session: Session,
    report_case: SeededReport,
) -> None:
    """端到端路径要校验、判定、把判定落库，再把报告入队。"""
    seed_published_sibling(session, report_case)

    outcome = publish_or_request_review(session, report_id=report_case.report_id, now=NOW)

    assert outcome.status == "scheduled"
    assert outcome.requires_review is False
    assert outcome.publish_at == NOW + timedelta(minutes=60)
    assert outcome.verification.passed is True
    assert outcome.pipeline_run_id is not None
    assert report_case.reload(session).publish_at == NOW + timedelta(minutes=60)
    assert int(session.scalar(select(func.count()).select_from(ReportVerification)) or 0) == 1
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs
    assert runs[0].started_at == NOW
    assert runs[0].finished_at == NOW


def test_publish_or_request_review_parks_a_manual_report(
    session: Session,
    workspace: Workspace,
) -> None:
    """转人工是正常结果，所以这次判定运行仍然算成功。"""
    case = _prepare_case(session, workspace, publish_mode=MANUAL)

    outcome = publish_or_request_review(session, report_id=case.report_id, now=NOW)

    assert outcome.status == "pending_review"
    assert outcome.requires_review is True
    assert outcome.publish_at is None
    assert case.reload(session).status == "pending_review"
    runs = list_pipeline_runs(session, run_type="publish")
    assert runs[0].status == "succeeded"
    assert runs[0].metrics_json is not None
    assert runs[0].metrics_json["resolution"] == "pending_review"


def test_publish_or_request_review_does_not_move_a_parked_report(
    session: Session,
    report_case: SeededReport,
) -> None:
    """重新评估一份已在等复核的报告，不能把它往前推。"""
    report = report_case.reload(session)
    report.status = "pending_review"
    session.commit()

    outcome = publish_or_request_review(session, report_id=report_case.report_id, now=NOW)

    assert outcome.status == "pending_review"
    assert report_case.reload(session).status == "pending_review"


def test_publish_or_request_review_refuses_terminal_states(
    session: Session,
    report_case: SeededReport,
) -> None:
    """失败或被退回的报告，必须重新生成后才能再次发布。"""
    report = report_case.reload(session)
    report.status = "failed"
    session.commit()

    with pytest.raises(ReportStateError, match="regenerate it"):
        publish_or_request_review(session, report_id=report_case.report_id, now=NOW)


# --------------------------------------------------------------------------------------
# 发布清扫
# --------------------------------------------------------------------------------------


def test_publish_due_reports_leaves_early_reports_alone(
    session: Session,
    report_case: SeededReport,
) -> None:
    """仍在窗口内的报告，清扫不会把它放出去。"""
    _force_scheduled(session, report_case, publish_at=NOW + timedelta(minutes=30))

    released = publish_due_reports(session, now=NOW)

    assert released == []
    assert report_case.reload(session).status == "scheduled"


def test_publish_due_reports_releases_due_reports(
    session: Session,
    report_case: SeededReport,
) -> None:
    """窗口一过，清扫就会把报告发布出去。"""
    _force_scheduled(session, report_case, publish_at=NOW - timedelta(minutes=1))

    released = publish_due_reports(session, now=NOW)

    assert len(released) == 1
    assert released[0]["report_id"] == report_case.report_id
    assert released[0]["status"] == "published"
    assert released[0]["error_code"] is None
    assert report_case.reload(session).status == "published"


def test_publish_due_reports_promotes_approved_reports(
    session: Session,
    report_case: SeededReport,
) -> None:
    """明确的批准会让撤回窗口在同一次清扫里被绕过。"""
    verify_report(session, report_id=report_case.report_id, now=NOW)
    report = report_case.reload(session)
    report.status = "approved"
    session.commit()

    released = publish_due_reports(session, now=NOW)

    assert len(released) == 1
    assert released[0]["status"] == "published"
    published = report_case.reload(session)
    assert published.status == "published"
    assert published.published_at == NOW


def test_publish_due_reports_marks_blocked_reports_and_continues(
    session: Session,
    workspace: Workspace,
) -> None:
    """一份拿不到判定，不该中止其它报告的发布。"""
    good = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    blocked = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    _force_scheduled(session, good, verify=True)
    _force_scheduled(session, blocked, verify=False)

    released = publish_due_reports(session, now=NOW)

    by_id = {entry["report_id"]: entry for entry in released}
    assert by_id[good.report_id]["status"] == "published"
    assert by_id[blocked.report_id]["status"] == "failed"
    assert by_id[blocked.report_id]["error_code"] == PublishPolicyBlocked.code
    assert blocked.reload(session).status == "failed"
    assert blocked.reload(session).publish_at is None
    failed_runs = [
        run for run in list_pipeline_runs(session, run_type="publish") if run.status == "failed"
    ]
    assert failed_runs
    assert failed_runs[0].error_code == PublishPolicyBlocked.code
    assert failed_runs[0].started_at == NOW
    assert failed_runs[0].finished_at == NOW


def test_publish_due_reports_rejects_a_non_positive_limit(
    session: Session,
) -> None:
    """清扫范围为零是编程错误，不是悄悄什么都不做。"""
    with pytest.raises(ValueError, match="limit must be at least 1"):
        publish_due_reports(session, now=NOW, limit=0)
