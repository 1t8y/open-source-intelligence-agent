"""Command-line entry point for report generation, verification, and publishing.

`--agent-run-id` on its own still means "generate", which keeps the T10 entry point
working; every later stage of the publishing chain is addressed with `--action`.
"""

import argparse
import json
import sys
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.core.config import get_settings
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

ACTIONS = ("generate", "verify", "publish", "review", "publish-due")
REVIEW_DECISIONS = ("approve", "reject", "request_changes")


def parse_args() -> argparse.Namespace:
    """Parse the action, its target, and the optional review payload."""
    parser = argparse.ArgumentParser(description="Generate, verify, and publish reports")
    parser.add_argument("--action", choices=ACTIONS, default="generate")
    parser.add_argument("--agent-run-id", type=int)
    parser.add_argument("--report-id", type=int)
    parser.add_argument("--title")
    parser.add_argument("--decision", choices=REVIEW_DECISIONS)
    parser.add_argument("--reviewer-id", type=int)
    parser.add_argument("--comment")
    parser.add_argument("--limit", type=int, default=PUBLISH_BATCH_LIMIT)
    return parser.parse_args()


def _required(value: int | None, flag: str) -> int:
    """Return a required numeric flag or exit with a usable message."""
    if value is None:
        raise SystemExit(f"{flag} is required for this action")
    return value


def run_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """Execute one CLI action against the given session."""
    if args.action == "publish-due":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        return {"released": publish_due_reports(session, limit=args.limit)}
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
    if args.decision is None:
        raise SystemExit("--decision is required for the review action")
    outcome = record_review(
        session,
        report_id=report_id,
        decision=args.decision,
        reviewer_id=args.reviewer_id,
        comment=args.comment,
    )
    return outcome.to_dict()


def main() -> None:
    """Run one action and print a JSON summary."""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            payload = run_action(args, session)
    except (ReportGenerationError, ReportPublishError) as exc:
        print(
            json.dumps(
                {"action": args.action, "error": str(exc)},
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
