"""在无人值守的情况下，把一次已完成的研究运行一路推进到结尾。

在这个模块出现之前，每日链路停在草稿上。`reports/cli.py --action generate` 能把草稿
变成报告，但没有任何东西按计划调用它，于是无人值守的一天最后只剩 `report_drafts` 行、
没有 `reports` 行 —— 既没有东西可验证，也没有东西可发布，更不会有邮件。「每天早上一封
周报」这句话，只在有人手动敲三条命令时才成立。

`finish_run_report` 就是缺失的那条边，它被留在 Worker 任务之外，因此可以脱离 Celery
测试，也能被 CLI 复用。它不放松任何一道关卡：报告仍然必须编排成功（每个带编号的章节
都需要一条可解析的指标引用），仍然必须通过验证，仍然要走发布策略 —— 证据单薄的运行会
以 `pending_review` 返回，而不是变成一份已发布的报告。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import ReportGenerationError
from open_source_intelligence.reports.publishing import publish_or_request_review
from open_source_intelligence.reports.verification import verify_report


def finish_run_report(session: Session, *, agent_run_id: int) -> dict[str, Any]:
    """把一次运行的草稿编排成报告，验证它，再套用发布策略。

    返回一份可直接转 JSON 的摘要而不是抛异常：草稿编排不成功本就是证据单薄时的正常
    结果，调用方（一个 Celery 任务）必须把它报上去，同时不能让任务失败 —— 那会触发
    重试，再烧一遍模型预算。
    """
    try:
        generated = generate_report(session, agent_run_id=agent_run_id)
        session.commit()
    except ReportGenerationError as exc:
        session.rollback()
        return {
            "stage": "generate",
            "report_id": None,
            "error_code": getattr(exc, "code", None),
            "error_message": str(exc),
        }

    verification = verify_report(session, report_id=generated.report_id)
    session.commit()
    summary: dict[str, Any] = {
        "stage": "verify",
        "report_id": generated.report_id,
        "verdict": verification.verdict,
        "reason_codes": list(verification.reason_codes),
    }
    if verification.verdict != "passed":
        # 报告已经存在，人现在可以查看它；发不发布由策略决定，而验证失败正是评审队列
        # 存在的理由。
        return summary

    outcome = publish_or_request_review(session, report_id=generated.report_id)
    session.commit()
    summary.update(
        {
            "stage": "publish",
            "status": outcome.status,
            "mode": outcome.mode,
            "requires_review": outcome.requires_review,
            "publish_at": outcome.publish_at.isoformat() if outcome.publish_at else None,
            "reason_codes": list(outcome.reason_codes),
        }
    )
    return summary
