"""Command-line entry point for resumable historical backfills.

The action set mirrors the job lifecycle rather than the HTTP surface: `start` creates and
walks a job, `resume` continues one from its cursor, and `status` / `list` report without
touching the source.
"""

import argparse
import json
from typing import Any

import httpx

from open_source_intelligence.backfill.errors import (
    BackfillJobBusy,
    BackfillLimitError,
    BackfillNotResumable,
    BackfillUnsupportedCollector,
    BackfillWalkFailed,
)
from open_source_intelligence.backfill.jobs import (
    DEFAULT_PER_PAGE,
    DEFAULT_TARGET_PAGES,
    MAX_TARGET_PAGES,
    get_job,
    list_jobs,
    summarize_job,
    summarize_jobs,
)
from open_source_intelligence.backfill.service import resume_backfill, start_backfill
from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.limiter import RateLimiter
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)


def parse_args() -> argparse.Namespace:
    """Parse the backfill action and its filters."""
    parser = argparse.ArgumentParser(
        description="Walk a provider's paged search history into the repository catalog"
    )
    parser.add_argument(
        "--action",
        choices=("start", "resume", "status", "list"),
        default="start",
    )
    parser.add_argument(
        "--provider",
        choices=("github", "huggingface"),
        default="github",
        help="A walk covers one source, so there is no 'all'",
    )
    parser.add_argument(
        "--target-pages",
        type=int,
        default=DEFAULT_TARGET_PAGES,
        help=f"Page budget for a new job, 1 to {MAX_TARGET_PAGES}",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=DEFAULT_PER_PAGE,
        help="Repositories requested per page",
    )
    parser.add_argument("--job-id", type=int, help="The job to resume or inspect")
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        help="Override the data source's own rate limit for this walk",
    )
    parser.add_argument("--job-status", help="Filter the job list by status")
    parser.add_argument("--limit", type=int, default=20, help="Job list page size")
    return parser.parse_args()


def _limiter(args: argparse.Namespace) -> RateLimiter | None:
    """Build an explicit limiter only when the operator overrode the source's own rate."""
    if args.requests_per_minute is None:
        return None
    return RateLimiter(per_minute=args.requests_per_minute)


def _read(args: argparse.Namespace) -> dict[str, Any]:
    """Answer `status` and `list` without touching a provider."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            if args.action == "status":
                if args.job_id is None:
                    raise SystemExit("--job-id is required for --action status")
                job = get_job(session, args.job_id)
                return {"job": None if job is None else summarize_job(job).to_dict()}
            jobs = list_jobs(
                session,
                provider=args.provider,
                status=args.job_status,
                limit=args.limit,
            )
            return {"jobs": summarize_jobs(jobs)}
    finally:
        engine.dispose()


def _walk(args: argparse.Namespace) -> dict[str, Any]:
    """Run or resume one walk against the real provider."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collector = build_collectors(args.provider, client)[0]
            with session_factory() as session:
                if args.action == "resume":
                    if args.job_id is None:
                        raise SystemExit("--job-id is required for --action resume")
                    outcome = resume_backfill(
                        session,
                        job_id=args.job_id,
                        collector=collector,
                        limiter=_limiter(args),
                    )
                else:
                    outcome = start_backfill(
                        session,
                        collector=collector,
                        target_pages=args.target_pages,
                        per_page=args.per_page,
                        limiter=_limiter(args),
                    )
                return outcome.to_dict()
    finally:
        engine.dispose()


def main() -> None:
    """Run the requested backfill operation and print a JSON payload."""
    args = parse_args()
    if args.action in {"status", "list"}:
        print(json.dumps(_read(args), ensure_ascii=False, sort_keys=True))
        return
    try:
        payload = _walk(args)
    except (
        BackfillNotResumable,
        BackfillJobBusy,
        BackfillLimitError,
        BackfillUnsupportedCollector,
    ) as exc:
        # These are operator mistakes rather than walk outcomes: nothing ran, so there is no
        # job payload to print and the message alone is the whole answer.
        raise SystemExit(str(exc)) from exc
    except BackfillWalkFailed as exc:
        # A failed walk's most valuable output is where the cursor stopped: the job stays
        # resumable, so the payload is the operator's next command rather than a traceback.
        settings = get_settings()
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as session:
                failed = get_job(session, exc.job_id)
                summary = None if failed is None else summarize_job(failed).to_dict()
        finally:
            engine.dispose()
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "job": summary,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
