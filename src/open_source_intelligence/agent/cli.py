"""Command-line entry point for the research Agent and its run history."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.agent.history import (
    DEFAULT_RUN_LIMIT,
    RUN_LIMIT_MAX,
    get_agent_run,
    list_agent_runs,
    list_agent_steps,
    list_tool_calls,
    summarize_agent_run,
    summarize_agent_steps,
    summarize_tool_calls,
)
from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.models.agent import AGENT_RUN_STATUSES
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def parse_args() -> argparse.Namespace:
    """Parse the Agent action and its filters."""
    parser = argparse.ArgumentParser(
        description="Run the research Agent and inspect Agent run history"
    )
    parser.add_argument("--action", choices=("run", "runs"), default="run")
    parser.add_argument("--topic-id", type=int, help="Topic to research")
    parser.add_argument("--run-id", type=int, help="Inspect one Agent run and its trail")
    parser.add_argument(
        "--status",
        choices=AGENT_RUN_STATUSES,
        help="Filter run history by status",
    )
    parser.add_argument("--run-limit", type=int, default=DEFAULT_RUN_LIMIT)
    parser.add_argument("--window-start", help="Research window start in ISO 8601")
    parser.add_argument("--window-end", help="Research window end in ISO 8601")
    parser.add_argument(
        "--max-candidates",
        type=int,
        help="Cap how many ranked candidates this run studies",
    )
    return parser.parse_args()


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 timestamp."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def run_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """Execute one research run and return its summary."""
    if args.topic_id is None:
        raise SystemExit("--topic-id is required for --action run")
    result = run_research_agent(
        session,
        topic_id=args.topic_id,
        settings=get_settings(),
        window_start=_parse_datetime(args.window_start),
        window_end=_parse_datetime(args.window_end),
        max_candidates=args.max_candidates,
    )
    return {"run": result.to_dict()}


def history_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """Return one run with its trail, or a filtered run history page."""
    if args.run_id is not None:
        run = get_agent_run(session, args.run_id)
        if run is None:
            return {"run": None, "steps": [], "tool_calls": []}
        return {
            "run": summarize_agent_run(run),
            "steps": summarize_agent_steps(list_agent_steps(session, run.id)),
            "tool_calls": summarize_tool_calls(list_tool_calls(session, run.id)),
        }
    runs = list_agent_runs(
        session,
        topic_id=args.topic_id,
        status=args.status,
        limit=args.run_limit,
    )
    return {"runs": [summarize_agent_run(run) for run in runs]}


def main() -> None:
    """Run the requested Agent operation and print a JSON payload."""
    args = parse_args()
    if not 1 <= args.run_limit <= RUN_LIMIT_MAX:
        raise SystemExit(f"--run-limit must be between 1 and {RUN_LIMIT_MAX}")
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            if args.action == "runs":
                payload = history_action(args, session)
            else:
                payload = run_action(args, session)
    finally:
        engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
