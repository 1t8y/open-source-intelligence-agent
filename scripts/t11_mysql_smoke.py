"""针对 MySQL 跑 T11 的校验器与发布检查。

脚本复用 T8 的种子辅助函数，然后把*真正的*报告 CLI 与真正的发布服务跑在 MySQL 上，
而不是内存 SQLite 里。它端到端覆盖 T11 的三条验收标准：

1. 伪造的引用会在任何东西发布之前被校验器抓住；
2. 干净的 `AUTO_WITH_DELAY` 报告只有熬过撤回窗口才会放行；
3. 没有审批记录的 `MANUAL` 报告根本无法发布。

发布时钟被钉在一个固定的未来时刻（与 T8、T10 冒烟一致），好让结果可复现。
发布服务会把这只钉住的时钟一并带进 `pipeline_runs` 的时间戳，否则
`finished_at >= started_at` 就会被违反。
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import t8_mysql_smoke as t8
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.errors import PublishPolicyBlocked
from open_source_intelligence.reports.publishing import (
    PUBLISH_MODE_MANUAL,
    publish_due_reports,
    publish_or_request_review,
    publish_report,
)
from open_source_intelligence.reports.verification import VERDICT_FAILED, VERDICT_PASSED

T11_NOW = datetime(2099, 6, 5, 12, 0, tzinfo=UTC)
FABRICATED_REF = "repo_snapshots:999999:2099-01-01"

#: 本冒烟写下的每份报告都用这个前缀做标题，清理时也按它匹配。匹配用的是**字面**前缀
#: （`startswith(..., autoescape=True)`）：`_` 在 `LIKE` 里是单字符通配符，所以想当然的
#: `title.like("__T11%")` 连标题 `abT11foo` 也会命中 —— 那份报告不是本冒烟写的，更没有
#: 理由被它删掉。标题与清理都读同一个常量，两者就不可能各走各的。
REPORT_TITLE_PREFIX = "__T11"

CLI = ("-m", "open_source_intelligence.reports.cli")


def run_cli(*args: str) -> dict[str, Any]:
    """跑一次真正的 CLI 调用，并解析它的 JSON 结果。"""
    completed = subprocess.run(
        [sys.executable, *CLI, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"report CLI {' '.join(args)} failed: {(completed.stderr or completed.stdout).strip()}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_cli_expecting_failure(*args: str) -> dict[str, Any]:
    """跑一次必须失败的 CLI 调用，并返回它的错误结果。"""
    completed = subprocess.run(
        [sys.executable, *CLI, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        raise RuntimeError(f"report CLI {' '.join(args)} unexpectedly succeeded")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _refresh(session: Session) -> None:
    """CLI 在另一个进程里写入之后，丢弃本地缓存的行。

    只调 `rollback()` 不够：已经加载过的实例仍攥着旧的属性值，后续查询可能返回一行，
    而它的 ORM 状态早已和 SQL 谓词对不上。生产环境每个任务用一个短生命周期会话，
    不会暴露在这个问题下。
    """
    session.rollback()
    session.expire_all()


def _published_sibling(session: Session, report: Report) -> Report:
    """插入一份已发布的同主题报告，让该主题不再是首次发布。"""
    sibling = Report(
        agent_run_id=report.agent_run_id,
        topic_id=report.topic_id,
        title=f"{REPORT_TITLE_PREFIX} prior weekly report__",
        current_version=1,
        content_markdown="# prior\n",
        status="published",
        visibility="INTERNAL",
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=T11_NOW,
        published_at=T11_NOW,
    )
    session.add(sibling)
    session.commit()
    return sibling


def _verification_failure_check(session: Session, report_id: int) -> dict[str, Any]:
    """判据 1：校验器拒掉无法解析的引用，然后接受修好的版本。"""
    source = session.scalar(
        select(ReportSource).where(ReportSource.report_id == report_id).order_by(ReportSource.id)
    )
    if source is None:
        raise RuntimeError("generated report has no citation rows")
    source_id = source.id
    original_ref = source.source_ref
    original_type = source.source_type
    original_snapshot = source.metric_snapshot_id

    source.source_type = "metric"
    source.source_ref = FABRICATED_REF
    source.metric_snapshot_id = None
    session.commit()

    failed = run_cli("--action", "verify", "--report-id", str(report_id))
    if failed["verdict"] != VERDICT_FAILED:
        raise RuntimeError(f"Verifier accepted a fabricated citation: {failed}")

    _refresh(session)
    restored_source = session.get(ReportSource, source_id)
    if restored_source is None:
        raise RuntimeError("citation row disappeared during verification check")
    restored_source.source_type = original_type
    restored_source.source_ref = original_ref
    restored_source.metric_snapshot_id = original_snapshot
    session.commit()

    passed = run_cli("--action", "verify", "--report-id", str(report_id))
    if passed["verdict"] != VERDICT_PASSED:
        raise RuntimeError(f"Verifier rejected the restored report: {passed}")
    _refresh(session)
    return {
        "failed_verdict": failed["verdict"],
        "failed_reasons": failed["reason_codes"],
        "failed_unresolved": failed["unresolved_refs"],
        "passed_verdict": passed["verdict"],
    }


def _delayed_release_check(
    session: Session,
    report_id: int,
    settings: Any,
) -> dict[str, Any]:
    """判据 2：干净的报告先等满窗口，然后才放行。"""
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before the delayed release check")
    _published_sibling(session, report)

    _refresh(session)
    decision = publish_or_request_review(
        session,
        report_id=report_id,
        settings=settings,
        now=T11_NOW,
    )
    early = publish_due_reports(session, now=T11_NOW)
    late = publish_due_reports(session, now=T11_NOW + timedelta(minutes=61))
    session.expire_all()
    released = session.get(Report, report_id)
    if released is None:
        raise RuntimeError("report vanished after the delayed release check")
    matched = [entry for entry in late if entry["report_id"] == report_id]
    window_minutes = settings.publish_delay_minutes
    return {
        "decision_status": decision.status,
        "decision_requires_review": decision.requires_review,
        "decision_publish_at": decision.publish_at.isoformat() if decision.publish_at else None,
        "expected_publish_at": (T11_NOW + timedelta(minutes=window_minutes)).isoformat(),
        "window_minutes": window_minutes,
        "early_release_count": len(early),
        "late_release_count": len(late),
        "late_released_status": matched[0]["status"] if matched else None,
        "final_status": released.status,
        "final_published_at": released.published_at.isoformat() if released.published_at else None,
    }


def _manual_approval_check(
    session: Session,
    report_id: int,
    settings: Any,
    reviewer_id: int,
) -> dict[str, Any]:
    """判据 3：`MANUAL` 没有审批记录就不许发布。"""
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before the manual approval check")
    report.status = "draft"
    report.publish_at = None
    report.published_at = None
    report.publish_mode = PUBLISH_MODE_MANUAL
    report.visibility = "INTERNAL"
    session.commit()

    review_request = run_cli("--action", "publish", "--report-id", str(report_id))

    _refresh(session)
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished after requesting review")
    report.status = "scheduled"
    report.publish_at = T11_NOW - timedelta(minutes=1)
    session.commit()

    blocked_code = ""
    try:
        publish_report(session, report_id=report_id, now=T11_NOW)
    except PublishPolicyBlocked as exc:
        blocked_code = exc.code
    _refresh(session)

    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished while restoring the review state")
    report.status = "pending_review"
    report.publish_at = None
    session.commit()

    approved = run_cli(
        "--action",
        "review",
        "--report-id",
        str(report_id),
        "--decision",
        "approve",
        "--reviewer-id",
        str(reviewer_id),
    )
    _refresh(session)
    due = publish_due_reports(session, now=T11_NOW)
    session.expire_all()
    final = session.get(Report, report_id)
    if final is None:
        raise RuntimeError("report vanished after approval")
    matched = [entry for entry in due if entry["report_id"] == report_id]
    return {
        "review_request_status": review_request["status"],
        "review_request_requires_review": review_request["requires_review"],
        "blocked_code": blocked_code,
        "approved_status": approved["status"],
        "released_count": len(matched),
        "released_status": matched[0]["status"] if matched else None,
        "final_status": final.status,
        "final_published_at": final.published_at.isoformat() if final.published_at else None,
    }


def run_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """让真正的 Agent、CLI 与发布服务走一遍 T11 的路径。"""
    settings = get_settings()
    agent_result = run_research_agent(
        session,
        topic_id=ids["topic_id"],
        settings=settings,
        now=t8.SMOKE_NOW,
    )
    if agent_result.report_draft_id is None:
        raise RuntimeError("Agent run did not create a report draft")

    generated = run_cli("--agent-run-id", str(agent_result.agent_run_id))
    report_id = int(generated["report_id"])
    _refresh(session)

    reviewer_id = session.scalar(select(User.id).where(User.username == t8.VERIFIER_USERNAME))
    if reviewer_id is None:
        raise RuntimeError("T8 verifier user was not seeded")

    verification = _verification_failure_check(session, report_id)
    delayed = _delayed_release_check(session, report_id, settings)
    manual = _manual_approval_check(session, report_id, settings, int(reviewer_id))

    session.expire_all()
    verification_rows = len(
        session.scalars(
            select(ReportVerification).where(ReportVerification.report_id == report_id)
        ).all()
    )
    review_rows = len(
        session.scalars(select(ReportReview).where(ReportReview.report_id == report_id)).all()
    )
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before summarizing")

    return {
        "agent_status": agent_result.status,
        "report_id": report_id,
        "report_status": report.status,
        "report_version": report.current_version,
        "verification_rows": verification_rows,
        "review_rows": review_rows,
        "verification": verification,
        "delayed": delayed,
        "manual": manual,
    }


def _cleanup(session: Session) -> None:
    """先删 T11 的校验与审批行，再删它们挂靠的 T8 记录。"""
    report_ids = select(Report.id).where(
        Report.title.startswith(REPORT_TITLE_PREFIX, autoescape=True)
    )
    session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
    session.execute(delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids)))
    session.execute(
        delete(Report).where(Report.title.startswith(REPORT_TITLE_PREFIX, autoescape=True))
    )
    session.commit()
    t8.cleanup_verification_records(session)


def main() -> None:
    """跑 T11 MySQL 冒烟，并打印结果。"""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            _cleanup(session)
            ids = t8.seed_topics(session)
            payload = run_checks(session, ids)
    finally:
        with session_factory() as session:
            _cleanup(session)
        engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
