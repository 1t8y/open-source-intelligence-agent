"""Command-line entry point for promoting one Agent draft into a report."""

import argparse
import json
import sys

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import ReportGenerationError


def parse_args() -> argparse.Namespace:
    """Parse the Agent run and optional title."""
    parser = argparse.ArgumentParser(description="Generate a report from one Agent draft")
    parser.add_argument("--agent-run-id", type=int, required=True)
    parser.add_argument("--title")
    return parser.parse_args()


def main() -> None:
    """Generate a report and print a JSON summary."""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            result = generate_report(
                session,
                agent_run_id=args.agent_run_id,
                title=args.title,
            )
    except ReportGenerationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True))
        engine.dispose()
        sys.exit(1)
    engine.dispose()
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
