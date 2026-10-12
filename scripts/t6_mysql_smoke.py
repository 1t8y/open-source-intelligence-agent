"""Run T6 pipeline run, scheduling, and retry smoke checks against MySQL."""

import argparse
import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError

from open_source_intelligence.collectors.cli import build_collectors
from open_source_intelligence.collectors.types import (
    CollectedRepository,
    RepositoryCollector,
    SourceUnavailableError,
)
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    PipelineRun,
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.pipeline.daily import PipelineStageError, run_daily_pipeline
from open_source_intelligence.pipeline.runs import list_pipeline_runs, summarize_pipeline_runs

TEMPORARY_USERNAME = "__t6_verifier__"
TEMPORARY_TOPIC_NAME = "__T6 Verification Topic__"
DISABLED_TOPIC_NAME = "__T6 Disabled Topic__"
VERIFY_DATE = date(2099, 2, 1)
SECRET_MARKER = "supersecretvalue"
BUSINESS_DATE_PATH = "$.business_date"
SNAPSHOT_DATE_PATH = "$.snapshot_date"


class FailingCollector:
    """Hugging Face collector double that always reports an unavailable source."""

    provider = "huggingface"
    display_name = "Hugging Face"
    base_url = "https://hf-mirror.com/api"

    def __init__(self, message: str) -> None:
        self._message = message

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        raise SourceUnavailableError(self._message)


class OfflineCollector:
    """GitHub collector double that succeeds without new data.

    It exists so the orchestration, run records, and constraints can be verified
    on a host without outbound network access. It never inserts repositories, so
    the snapshot and rank stages still work on the real data collected by T4.
    """

    provider = "github"
    display_name = "GitHub"
    base_url = "https://api.github.com"

    def collect(self, *, limit: int) -> list[CollectedRepository]:
        return []


def cleanup_verification_records(session: Any, *, extra_dates: Sequence[date] = ()) -> None:
    """Remove verification topics, snapshots, and runs for the verification dates.

    Global collect and snapshot runs carry their business date in ``metrics_json``,
    so an interrupted previous run, or a run created by the Celery checks, is
    still cleaned up here.
    """
    dates = [VERIFY_DATE, *extra_dates]
    date_strings = [item.isoformat() for item in dates]
    session.execute(
        text(
            "DELETE FROM topic_repositories WHERE topic_id IN "
            "(SELECT id FROM topics WHERE name IN (:a, :b))"
        ),
        {"a": TEMPORARY_TOPIC_NAME, "b": DISABLED_TOPIC_NAME},
    )
    session.execute(
        text(
            "DELETE FROM pipeline_runs WHERE topic_id IN "
            "(SELECT id FROM topics WHERE name IN (:a, :b))"
        ),
        {"a": TEMPORARY_TOPIC_NAME, "b": DISABLED_TOPIC_NAME},
    )
    session.execute(
        delete(PipelineRun).where(
            PipelineRun.topic_id.is_(None),
            func.json_extract(PipelineRun.metrics_json, BUSINESS_DATE_PATH).in_(date_strings),
        )
    )
    session.execute(
        delete(PipelineRun).where(
            PipelineRun.topic_id.is_(None),
            func.json_extract(PipelineRun.metrics_json, SNAPSHOT_DATE_PATH).in_(date_strings),
        )
    )
    session.execute(
        text("DELETE FROM topics WHERE name IN (:a, :b)"),
        {"a": TEMPORARY_TOPIC_NAME, "b": DISABLED_TOPIC_NAME},
    )
    session.execute(text("DELETE FROM users WHERE username = :name"), {"name": TEMPORARY_USERNAME})
    session.execute(delete(RepoSnapshot).where(RepoSnapshot.snapshot_date.in_(dates)))
    session.commit()


def add_topic(session: Any, *, user_id: int, name: str, enabled: bool) -> Topic:
    """Insert one verification topic bound to the temporary user."""
    topic = Topic(
        name=name,
        languages_json=None,
        keywords_json=None,
        min_stars=0,
        window_days=7,
        max_candidates=50,
        publish_mode="AUTO_WITH_DELAY",
        enabled=enabled,
        created_by=user_id,
    )
    session.add(topic)
    session.commit()
    return topic


def check_topic_scope_constraint(session: Any) -> bool:
    """Confirm the database rejects an Agent run that has no topic."""
    try:
        session.execute(
            text(
                "INSERT INTO pipeline_runs (topic_id, run_type, status, attempt) "
                "VALUES (NULL, 'agent', 'queued', 1)"
            )
        )
    except DBAPIError:
        session.rollback()
        return True
    session.rollback()
    return False


def parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T6 MySQL smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove verification records and exit",
    )
    parser.add_argument(
        "--extra-cleanup-dates",
        nargs="*",
        default=[],
        help="Additional business dates to clean alongside the verification date",
    )
    parser.add_argument(
        "--collector",
        choices=("real", "offline"),
        default="real",
        help="Use the real GitHub API, or an offline double for hosts without egress",
    )
    return parser.parse_args()


def main() -> None:
    """Verify run records, retry trail, status queries, and constraints on MySQL."""
    args = parse_args()
    extra_dates = [date.fromisoformat(item) for item in args.extra_cleanup_dates]
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    marker_id = 0
    result: dict[str, Any] = {}

    if args.cleanup_only:
        with session_factory() as session:
            cleanup_verification_records(session, extra_dates=extra_dates)
        engine.dispose()
        print(
            json.dumps(
                {
                    "cleanup_only": True,
                    "verification_date": VERIFY_DATE.isoformat(),
                    "extra_dates": [item.isoformat() for item in extra_dates],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    try:
        with session_factory() as session:
            cleanup_verification_records(session, extra_dates=extra_dates)
            repository_count = session.scalar(select(func.count(Repository.id))) or 0
            if repository_count == 0:
                raise RuntimeError("T6 verification requires repositories from T4")
            marker_id = session.scalar(select(func.coalesce(func.max(PipelineRun.id), 0))) or 0

            user = User(
                username=TEMPORARY_USERNAME,
                password_hash="t6-verification-only",
                display_name="T6 Verifier",
                status="active",
                data_scope="team",
            )
            session.add(user)
            session.commit()
            user_id = user.id
            topic = add_topic(session, user_id=user_id, name=TEMPORARY_TOPIC_NAME, enabled=True)
            disabled_topic = add_topic(
                session,
                user_id=user_id,
                name=DISABLED_TOPIC_NAME,
                enabled=False,
            )
            topic_id = topic.id
            disabled_topic_id = disabled_topic.id

        with httpx.Client(timeout=20, follow_redirects=True) as client:
            collectors: list[RepositoryCollector] = (
                build_collectors("github", client)
                if args.collector == "real"
                else [OfflineCollector()]
            )
            with session_factory() as session:
                happy = run_daily_pipeline(
                    session,
                    business_date=VERIFY_DATE,
                    collectors=collectors,
                    limit=2,
                )
                happy_runs = session.scalars(
                    select(PipelineRun).where(PipelineRun.id > marker_id).order_by(PipelineRun.id)
                ).all()
                happy_snapshot_rows = session.scalar(
                    select(func.count(RepoSnapshot.id)).where(
                        RepoSnapshot.snapshot_date == VERIFY_DATE
                    )
                )
                disabled_rank_runs = session.scalar(
                    select(func.count(PipelineRun.id)).where(
                        PipelineRun.topic_id == disabled_topic_id
                    )
                )
                repeated = run_daily_pipeline(
                    session,
                    business_date=VERIFY_DATE,
                    collectors=collectors,
                    limit=2,
                )
                repeated_snapshot_rows = session.scalar(
                    select(func.count(RepoSnapshot.id)).where(
                        RepoSnapshot.snapshot_date == VERIFY_DATE
                    )
                )
                repeated_candidates = session.scalar(
                    select(func.count(TopicRepository.topic_id)).where(
                        TopicRepository.topic_id == topic_id
                    )
                )
                verification_run_rows = session.scalar(
                    select(func.count(PipelineRun.id)).where(PipelineRun.id > marker_id)
                )

        if happy.status != "succeeded":
            raise RuntimeError(f"unexpected happy path status: {happy.status}")
        if [stage.run_type for stage in happy.stages] != ["collect", "snapshot", "rank"]:
            raise RuntimeError(f"unexpected stage order: {[s.run_type for s in happy.stages]}")
        if happy.snapshot_count != repository_count:
            raise RuntimeError(
                f"snapshot count mismatch: expected {repository_count}, got {happy.snapshot_count}"
            )
        if happy.topic_candidate_counts != {str(topic_id): repository_count}:
            raise RuntimeError(f"unexpected candidate counts: {happy.topic_candidate_counts}")
        if happy.provider_counts.get("github", 0) < 1 and args.collector == "real":
            raise RuntimeError(f"real GitHub collection returned nothing: {happy.provider_counts}")
        if [run.status for run in happy_runs] != ["succeeded", "succeeded", "succeeded"]:
            raise RuntimeError(f"unexpected run statuses: {[run.status for run in happy_runs]}")
        if [run.attempt for run in happy_runs] != [1, 1, 1]:
            raise RuntimeError(
                f"unexpected first attempt values: {[run.attempt for run in happy_runs]}"
            )
        if happy_snapshot_rows != repository_count:
            raise RuntimeError(f"unexpected snapshot rows: {happy_snapshot_rows}")
        if repeated.status != "succeeded" or repeated_snapshot_rows != happy_snapshot_rows:
            raise RuntimeError("rerun duplicated snapshot rows")
        if repeated_candidates != repository_count:
            raise RuntimeError("rerun duplicated candidate rows")
        if disabled_rank_runs != 0:
            raise RuntimeError("disabled topic received a rank run")
        if verification_run_rows != 6:
            raise RuntimeError(
                f"expected one run row per stage per attempt, got {verification_run_rows}"
            )

        with session_factory() as session:
            scope_enforced = check_topic_scope_constraint(session)

        with session_factory() as session:
            failing = FailingCollector(f"source unavailable token={SECRET_MARKER}")
            try:
                run_daily_pipeline(
                    session,
                    business_date=VERIFY_DATE,
                    collectors=[failing],
                    limit=2,
                    attempt=3,
                )
            except PipelineStageError as exc:
                failure_code = exc.error_code
            else:
                raise RuntimeError("failing collector did not stop the batch")

            failed_run = session.scalar(
                select(PipelineRun)
                .where(PipelineRun.run_type == "collect", PipelineRun.attempt == 3)
                .order_by(PipelineRun.id.desc())
            )
            if failed_run is None:
                raise RuntimeError("failed collect run was not recorded")
            failed_status = failed_run.status
            failed_message = failed_run.error_message or ""
            snapshot_runs_after_failure = session.scalar(
                select(func.count(PipelineRun.id)).where(
                    PipelineRun.run_type == "snapshot",
                    PipelineRun.attempt == 3,
                )
            )
            history = summarize_pipeline_runs(list_pipeline_runs(session, limit=5))
            failed_only = summarize_pipeline_runs(list_pipeline_runs(session, status="failed"))
            attempt_trail = sorted(
                {
                    run.attempt
                    for run in session.scalars(
                        select(PipelineRun).where(
                            PipelineRun.run_type == "collect",
                            PipelineRun.id > marker_id,
                        )
                    ).all()
                }
            )

        if failure_code != "COLLECTOR_FAILED":
            raise RuntimeError(f"unexpected failure code: {failure_code}")
        if failed_status != "failed":
            raise RuntimeError(f"unexpected failed status: {failed_status}")
        if SECRET_MARKER in failed_message or "token=[REDACTED]" not in failed_message:
            raise RuntimeError(f"error message was not redacted: {failed_message}")
        if snapshot_runs_after_failure != 0:
            raise RuntimeError("batch continued after a total collection failure")
        if not scope_enforced:
            raise RuntimeError("pipeline_runs topic scope CHECK constraint is missing")
        if not failed_only or not all(item["status"] == "failed" for item in failed_only):
            raise RuntimeError("status filter did not return only failed runs")
        if attempt_trail != [1, 3]:
            raise RuntimeError(f"unexpected attempt trail: {attempt_trail}")

        result = {
            "collector_mode": args.collector,
            "verification_date": VERIFY_DATE.isoformat(),
            "repository_count": repository_count,
            "happy_status": happy.status,
            "happy_run_types": [stage.run_type for stage in happy.stages],
            "happy_run_statuses": [run.status for run in happy_runs],
            "happy_attempts": [run.attempt for run in happy_runs],
            "provider_counts": happy.provider_counts,
            "snapshot_count": happy.snapshot_count,
            "topic_candidate_counts": happy.topic_candidate_counts,
            "snapshot_rows_per_date": happy_snapshot_rows,
            "rerun_status": repeated.status,
            "rerun_snapshot_rows": repeated_snapshot_rows,
            "rerun_candidate_rows": repeated_candidates,
            "verification_run_rows": verification_run_rows,
            "disabled_topic_rank_runs": disabled_rank_runs,
            "topic_scope_constraint_enforced": scope_enforced,
            "failure_code": failure_code,
            "failure_status": failed_status,
            "failure_attempt": failed_run.attempt,
            "failure_message_redacted": SECRET_MARKER not in failed_message,
            "snapshot_runs_after_failure": snapshot_runs_after_failure,
            "attempt_trail": attempt_trail,
            "history_sample": [item["run_type"] + ":" + item["status"] for item in history],
        }
    finally:
        with session_factory() as session:
            cleanup_verification_records(session, extra_dates=extra_dates)
            if marker_id:
                session.execute(delete(PipelineRun).where(PipelineRun.id > marker_id))
                session.commit()
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
