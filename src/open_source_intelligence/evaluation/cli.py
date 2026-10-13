"""Command-line entry point for importing datasets, running evaluations, and comparing.

`--action run` is the one that actually executes the Agent; `list` and `compare` are read-only
and exist so an operator can inspect a dataset and its last comparison without touching the API.
Every action prints one JSON document, matching the other CLIs in this codebase.
"""

from __future__ import annotations

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

from .dataset import (
    DEFAULT_DATASET_DIR,
    DatasetSpec,
    discover_dataset_files,
    import_dataset,
    list_cases,
    list_datasets,
    load_dataset_file,
)
from .errors import EvaluationError
from .runner import compare_latest, run_evaluation

ACTIONS = ("list", "import", "run", "compare")


def parse_args() -> argparse.Namespace:
    """Parse the action, its dataset, and the optional target selection."""
    parser = argparse.ArgumentParser(description="Import and run offline evaluations")
    parser.add_argument("--action", choices=ACTIONS, default="list")
    parser.add_argument("--dataset-file")
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    parser.add_argument("--dataset-id", type=int)
    parser.add_argument("--name")
    parser.add_argument("--version")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="repeatable; defaults to every target variant",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--actor-id", type=int)
    return parser.parse_args()


def _load_specs(args: argparse.Namespace) -> list[DatasetSpec]:
    """Return the dataset documents to import."""
    if args.dataset_file:
        return [load_dataset_file(args.dataset_file)]
    paths = discover_dataset_files(args.dataset_dir)
    if not paths:
        raise SystemExit(f"no dataset file found in {args.dataset_dir!r}")
    return [load_dataset_file(path) for path in paths]


def run_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """Execute one CLI action against the given session."""
    if args.action == "list":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        return {
            "items": [
                {
                    "dataset_id": row.id,
                    "name": row.name,
                    "version": row.version,
                    "description": row.description,
                    "case_count": len(list_cases(session, dataset_id=row.id)),
                }
                for row in list_datasets(session, limit=args.limit)
            ]
        }
    if args.action == "import":
        results = [
            import_dataset(session, spec, created_by=args.actor_id) for spec in _load_specs(args)
        ]
        return {"imported": [result.to_dict() for result in results]}
    if args.action == "run":
        report = run_evaluation(
            session,
            dataset_id=args.dataset_id,
            name=args.name,
            version=args.version,
            target_types=args.targets,
        )
        return report.to_dict()
    if args.dataset_id is None:
        raise SystemExit("--dataset-id is required for the compare action")
    return compare_latest(session, dataset_id=args.dataset_id).to_dict()


def main() -> None:
    """Run one action and print a JSON summary."""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            payload = run_action(args, session)
    except EvaluationError as exc:
        # The stable `code` lets a script branch on the failure without matching on message text,
        # exactly as the API answers with it.
        print(
            json.dumps(
                {"action": args.action, "code": exc.code, "error": str(exc)},
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
