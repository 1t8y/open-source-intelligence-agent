"""Run T5 snapshot and ranking CLI smoke checks against MySQL."""

import json
import subprocess
import sys
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)

TEMPORARY_USERNAME = "__t5_verifier__"
TEMPORARY_TOPIC_NAME = "__T5 Verification Topic__"
SNAPSHOT_DATES = (date(2099, 1, 1), date(2099, 1, 2))


def run_pipeline_cli(action: str, business_date: date) -> dict[str, Any]:
    """Execute the real pipeline CLI and parse its JSON output."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_source_intelligence.pipeline.cli",
            "--action",
            action,
            "--date",
            business_date.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"pipeline CLI failed for {action}: {completed.stderr.strip()}")
    output = completed.stdout.strip().splitlines()
    if not output:
        raise RuntimeError(f"pipeline CLI returned no output for {action}")
    return json.loads(output[-1])


def cleanup_verification_records(session: Any) -> None:
    """Remove records created by previous or current T5 verification runs."""
    session.execute(delete(RepoSnapshot).where(RepoSnapshot.snapshot_date.in_(SNAPSHOT_DATES)))
    topic = session.scalar(select(Topic).where(Topic.name == TEMPORARY_TOPIC_NAME))
    if topic is not None:
        session.execute(delete(TopicRepository).where(TopicRepository.topic_id == topic.id))
        session.delete(topic)
    user = session.scalar(select(User).where(User.username == TEMPORARY_USERNAME))
    if user is not None:
        session.delete(user)
    session.commit()


def main() -> None:
    """Verify CLI idempotency, deltas, ranking, and audit records on MySQL."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        cleanup_verification_records(session)
        repository_count = session.scalar(select(func.count(Repository.id))) or 0
        if repository_count == 0:
            raise RuntimeError("T5 verification requires repositories from T4")

        user = User(
            username=TEMPORARY_USERNAME,
            password_hash="t5-verification-only",
            display_name="T5 Verifier",
            status="active",
            data_scope="team",
        )
        session.add(user)
        session.flush()
        topic = Topic(
            name=TEMPORARY_TOPIC_NAME,
            languages_json=None,
            keywords_json=None,
            min_stars=0,
            window_days=7,
            max_candidates=repository_count,
            publish_mode="AUTO_WITH_DELAY",
            enabled=True,
            created_by=user.id,
        )
        session.add(topic)
        session.commit()
        topic_id = topic.id
        user_id = user.id

    try:
        first_output = run_pipeline_cli("snapshot", SNAPSHOT_DATES[0])
        second_output = run_pipeline_cli("snapshot", SNAPSHOT_DATES[1])
        repeated_output = run_pipeline_cli("snapshot", SNAPSHOT_DATES[1])
        first_rank_output = run_pipeline_cli("rank", SNAPSHOT_DATES[1])
        second_rank_output = run_pipeline_cli("rank", SNAPSHOT_DATES[1])

        with session_factory() as session:
            snapshot_rows = {
                snapshot_date.isoformat(): session.scalar(
                    select(func.count(RepoSnapshot.id)).where(
                        RepoSnapshot.snapshot_date == snapshot_date
                    )
                )
                for snapshot_date in SNAPSHOT_DATES
            }
            second_day_deltas = session.scalars(
                select(RepoSnapshot.stars_delta).where(
                    RepoSnapshot.snapshot_date == SNAPSHOT_DATES[1]
                )
            ).all()
            candidate_rows = session.scalars(
                select(TopicRepository)
                .where(TopicRepository.topic_id == topic_id)
                .order_by(TopicRepository.rank)
            ).all()
            audit_count = session.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.action.in_(("snapshot.create", "candidate.rank"))
                )
            )

        if any(count != repository_count for count in snapshot_rows.values()):
            raise RuntimeError(
                f"unexpected snapshot rows: expected {repository_count}, got {snapshot_rows}"
            )
        if second_day_deltas != [0] * repository_count:
            raise RuntimeError(f"unexpected second-day deltas: {second_day_deltas}")
        if len(candidate_rows) != repository_count:
            raise RuntimeError(
                f"unexpected candidate rows: expected {repository_count}, got {len(candidate_rows)}"
            )
        if [row.rank for row in candidate_rows] != list(range(1, repository_count + 1)):
            raise RuntimeError(f"candidate ranks are not contiguous: {candidate_rows}")
        if not all(row.selected_for_agent for row in candidate_rows):
            raise RuntimeError("verification topic did not select all candidates")
        if (audit_count or 0) < 5:
            raise RuntimeError(f"expected at least 5 T5 audit records, got {audit_count}")

        result = {
            "repository_count": repository_count,
            "snapshot_rows_per_date": snapshot_rows,
            "second_day_deltas": second_day_deltas,
            "candidate_count": len(candidate_rows),
            "snapshot_cli_counts": [
                first_output["snapshot_count"],
                second_output["snapshot_count"],
                repeated_output["snapshot_count"],
            ],
            "topic_candidate_counts": [
                first_rank_output["topic_candidate_counts"].get(str(topic_id)),
                second_rank_output["topic_candidate_counts"].get(str(topic_id)),
            ],
            "audit_count": audit_count,
        }
    finally:
        with session_factory() as session:
            cleanup_verification_records(session)
            session.execute(delete(Topic).where(Topic.id == topic_id))
            session.execute(delete(User).where(User.id == user_id))
            session.commit()
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
