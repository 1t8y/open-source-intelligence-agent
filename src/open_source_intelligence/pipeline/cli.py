"""Command-line entry point for snapshots and candidate ranking."""

import argparse
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.ranking import rank_all_topics
from open_source_intelligence.pipeline.snapshots import create_daily_snapshots


def parse_args() -> argparse.Namespace:
    """Parse pipeline action and optional date."""
    parser = argparse.ArgumentParser(description="Run deterministic snapshot and ranking jobs")
    parser.add_argument("--action", choices=("snapshot", "rank", "all"), default="all")
    parser.add_argument("--date", help="Business date in YYYY-MM-DD")
    return parser.parse_args()


def resolve_date(value: str | None) -> date:
    """Resolve a supplied date or the current date in the configured timezone."""
    if value:
        return date.fromisoformat(value)
    timezone = ZoneInfo(get_settings().app_timezone)
    return datetime.now(timezone).date()


def main() -> None:
    """Run the requested deterministic pipeline operation."""
    args = parse_args()
    business_date = resolve_date(args.date)
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    result: dict[str, Any] = {"date": business_date.isoformat()}
    with session_factory() as session:
        if args.action in {"snapshot", "all"}:
            result["snapshot_count"] = create_daily_snapshots(session, snapshot_date=business_date)
        if args.action in {"rank", "all"}:
            result["topic_candidate_counts"] = rank_all_topics(session, as_of_date=business_date)
    engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
