"""提供报告生成、验证、发布与推送的命令行入口。

单独给 `--agent-run-id` 仍然表示「生成」，T10 的入口因此照旧可用；发布链路上后续的每
个阶段都用 `--action` 指定。
"""

import argparse
import json
import sys
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.notifications.errors import PushError
from open_source_intelligence.notifications.service import push_report
from open_source_intelligence.notifications.service import (
    retry_failed_pushes as execute_retry_failed_pushes,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import (
    ReportGenerationError,
    ReportPublishError,
)
from open_source_intelligence.reports.publishing import (
    PUBLISH_BATCH_LIMIT,
    publish_due_reports,
    publish_or_request_review,
    record_review,
)
from open_source_intelligence.reports.verification import verify_report

ACTIONS = ("generate", "verify", "publish", "review", "publish-due", "push", "push-retry")
REVIEW_DECISIONS = ("approve", "reject", "request_changes")
PUSH_CHANNELS = ("email", "webhook")


def parse_args() -> argparse.Namespace:
    """解析动作、动作的目标，以及可选的评审载荷。"""
    parser = argparse.ArgumentParser(description="Generate, verify, publish, and push reports")
    parser.add_argument("--action", choices=ACTIONS, default="generate")
    parser.add_argument("--agent-run-id", type=int)
    parser.add_argument("--report-id", type=int)
    parser.add_argument("--title")
    parser.add_argument("--decision", choices=REVIEW_DECISIONS)
    parser.add_argument("--reviewer-id", type=int)
    parser.add_argument("--comment")
    parser.add_argument("--limit", type=int, default=PUBLISH_BATCH_LIMIT)
    parser.add_argument("--channel", choices=PUSH_CHANNELS)
    parser.add_argument("--target-id", type=int)
    return parser.parse_args()


def _required(value: int | None, flag: str) -> int:
    """返回必填的数字选项，缺失时以可读的提示退出。"""
    if value is None:
        raise SystemExit(f"{flag} is required for this action")
    return value


def run_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """在给定会话上执行一个 CLI 动作。"""
    if args.action == "publish-due":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        return {"released": publish_due_reports(session, limit=args.limit)}
    if args.action == "push-retry":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        outcomes = execute_retry_failed_pushes(session, limit=args.limit)
        return {
            "limit": args.limit,
            "attempted_count": len(outcomes),
            "delivered_count": sum(1 for item in outcomes if item.delivered),
            "outcomes": [item.to_dict() for item in outcomes],
        }
    if args.action == "generate":
        result = generate_report(
            session,
            agent_run_id=_required(args.agent_run_id, "--agent-run-id"),
            title=args.title,
        )
        return result.to_dict()

    report_id = _required(args.report_id, "--report-id")
    if args.action == "verify":
        return verify_report(session, report_id=report_id).to_dict()
    if args.action == "publish":
        return publish_or_request_review(session, report_id=report_id).to_dict()
    if args.action == "push":
        if args.channel is None:
            raise SystemExit("--channel is required for the push action")
        push_outcome = push_report(
            session,
            report_id=report_id,
            channel=args.channel,
            target_id=_required(args.target_id, "--target-id"),
            actor_id=args.reviewer_id,
        )
        return push_outcome.to_dict()
    if args.decision is None:
        raise SystemExit("--decision is required for the review action")
    review_outcome = record_review(
        session,
        report_id=report_id,
        decision=args.decision,
        reviewer_id=args.reviewer_id,
        comment=args.comment,
    )
    return review_outcome.to_dict()


def main() -> None:
    """执行一个动作并打印 JSON 摘要。"""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            payload = run_action(args, session)
    except (ReportGenerationError, ReportPublishError, PushError) as exc:
        # `code` 与 API 应答里的稳定标识符同源，脚本可以按失败分支，而不必匹配消息文本。
        # 少数自身不带 code 的错误，这里仍然是 `null`。
        print(
            json.dumps(
                {"action": args.action, "code": getattr(exc, "code", None), "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        engine.dispose()
        sys.exit(1)
    engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
