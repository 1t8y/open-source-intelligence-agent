"""Command-line entry point for the deterministic pipeline and run history."""

import argparse
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from open_source_intelligence.collectors.cli import build_collectors, build_content_sources
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.contents import business_day_start, run_contents_stage
from open_source_intelligence.pipeline.daily import run_daily_pipeline, run_rank_all_topics
from open_source_intelligence.pipeline.runs import (
    get_pipeline_run,
    list_pipeline_runs,
    summarize_pipeline_run,
    summarize_pipeline_runs,
)
from open_source_intelligence.pipeline.snapshots import create_daily_snapshots


def parse_args() -> argparse.Namespace:
    """Parse the pipeline action and its filters."""
    parser = argparse.ArgumentParser(
        description="Run deterministic pipeline jobs and inspect pipeline run history"
    )
    parser.add_argument(
        "--action",
        choices=("snapshot", "rank", "contents", "all", "daily", "runs"),
        default="all",
    )
    parser.add_argument("--date", help="Business date in YYYY-MM-DD")
    parser.add_argument("--provider", choices=("github", "huggingface", "all"), default="all")
    parser.add_argument("--limit", type=int, default=5, help="Repositories per provider")
    parser.add_argument(
        "--content-limit",
        type=int,
        help="Repositories whose content is pre-warmed (default: PIPELINE_CONTENT_LIMIT)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the content cache window and ask about every candidate again",
    )
    parser.add_argument(
        "--run-type",
        choices=("collect", "snapshot", "rank", "contents", "agent", "publish"),
        help="Filter run history by run type",
    )
    parser.add_argument(
        "--status",
        choices=("queued", "running", "succeeded", "failed", "partial"),
        help="Filter run history by status",
    )
    parser.add_argument("--topic-id", type=int, help="Filter run history by topic")
    parser.add_argument("--run-id", type=int, help="Inspect one pipeline run")
    parser.add_argument("--run-limit", type=int, default=20, help="Run history page size")
    return parser.parse_args()


def resolve_date(value: str | None) -> date:
    """Resolve a supplied date or the current date in the configured timezone."""
    if value:
        return date.fromisoformat(value)
    timezone = ZoneInfo(get_settings().app_timezone)
    return datetime.now(timezone).date()


def run_history(args: argparse.Namespace) -> dict[str, Any]:
    """Return one run or a filtered run history page."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            if args.run_id is not None:
                run = get_pipeline_run(session, args.run_id)
                return {"run": None if run is None else summarize_pipeline_run(run).to_dict()}
            runs = list_pipeline_runs(
                session,
                topic_id=args.topic_id,
                run_type=args.run_type,
                status=args.status,
                limit=args.run_limit,
            )
            return {"runs": summarize_pipeline_runs(runs)}
    finally:
        engine.dispose()


def resolve_content_limit(args: argparse.Namespace, settings: object) -> int:
    """Return the content pre-warm bound, from the flag or the configured default."""
    configured = getattr(settings, "pipeline_content_limit", None)
    limit = args.content_limit if args.content_limit is not None else configured
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SystemExit("--content-limit must be between 1 and 100")
    return limit


def run_deterministic_action(args: argparse.Namespace, business_date: date) -> dict[str, Any]:
    """Execute one deterministic action and return its JSON payload."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    refresh_after = None if args.force else business_day_start(business_date, settings.app_timezone)
    try:
        if args.action == "daily":
            if not 1 <= args.limit <= 100:
                raise SystemExit("--limit must be between 1 and 100")
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                collectors = build_collectors(args.provider, client)
                content_sources = build_content_sources(args.provider, client)
                with session_factory() as session:
                    result = run_daily_pipeline(
                        session,
                        business_date=business_date,
                        collectors=collectors,
                        limit=args.limit,
                        content_sources=content_sources,
                        content_limit=resolve_content_limit(args, settings),
                        content_refresh_after=refresh_after,
                    )
            return result.to_dict()
        if args.action == "contents":
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                content_sources = build_content_sources(args.provider, client)
                if not content_sources:
                    raise SystemExit(f"provider {args.provider!r} cannot serve repository content")
                with session_factory() as session:
                    outcome = run_contents_stage(
                        session,
                        business_date=business_date,
                        sources=content_sources,
                        limit=resolve_content_limit(args, settings),
                        refresh_after=refresh_after,
                    )
            return {
                "date": business_date.isoformat(),
                "run_id": outcome.stage.run_id,
                "status": outcome.stage.status,
                "candidate_count": outcome.candidate_count,
                "content_counts": {
                    "fetched": outcome.summary.fetched,
                    "skipped": outcome.summary.skipped,
                    "missing": outcome.summary.missing,
                    "unsupported": outcome.summary.unsupported,
                    "failed": outcome.summary.failed,
                },
            }
        payload: dict[str, Any] = {"date": business_date.isoformat()}
        with session_factory() as session:
            if args.action in {"snapshot", "all"}:
                payload["snapshot_count"] = create_daily_snapshots(
                    session,
                    snapshot_date=business_date,
                )
            if args.action in {"rank", "all"}:
                rank_stages = run_rank_all_topics(session, business_date=business_date)
                payload["topic_candidate_counts"] = {
                    str(stage.topic_id): int(stage.metrics.get("candidate_count") or 0)
                    for stage in rank_stages
                    if stage.status == "succeeded"
                }
                payload["rank_run_ids"] = [stage.run_id for stage in rank_stages]
        return payload
    finally:
        engine.dispose()


def main() -> None:
    """Run the requested pipeline operation and print a JSON payload."""
    args = parse_args()
    if args.action == "runs":
        payload = run_history(args)
    else:
        payload = run_deterministic_action(args, resolve_date(args.date))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
