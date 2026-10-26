"""Turn an empty deployment into a published demo report in one idempotent pass.

`scripts/seed_demo.py` stops at the candidate layer — accounts, source, topic, ranked
repositories, snapshots, README cache, push targets, and the two boundary shapes the evaluation
dataset keeps disabled. That is the data; this script drives the *pipeline* on top of it so the
console opens onto a real, published report rather than an empty ledger.

The pass runs the deterministic half with the offline model (`AGENT_PROVIDER=offline`), so it
needs no network and no credential and produces the same report every time. It does **not** run
`run_daily_pipeline` (collect → snapshot → rank → contents): the seed already wrote those rows by
hand, and re-collecting would need a reachable source. What it runs is everything downstream of
ranking, which is exactly what a demo has to prove:

    research (offline Agent) → generate → verify → approve → publish → auto-push

Because `AUTO_WITH_DELAY` reports queue behind a withdrawal window, the script records the human
`approve` and then releases it — the same documented path an operator walks, so the demo shows a
report that reached `published` honestly rather than by a bypass.

It is idempotent in the same sense as the seeder: every stage reuses the row a prior run created,
so re-running advances nothing and mints no duplicate. If the topic already has a `published`
report, the pass reports it and returns without creating a new agent run or report. Run inside
the api container after the seeder:

    docker compose --env-file .env -f infra/docker-compose.yml exec -T api \
      python /app/scripts/init_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

# `scripts/` is invoked as a file inside the container (`python /app/scripts/init_demo.py`),
# where `scripts` is not an installed package; make the sibling seeder importable by name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_demo as seed  # noqa: E402

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    record_review,
)
from open_source_intelligence.security.service import get_user_by_username

_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "dev-admin-password-change-me")
_USER_PASSWORD = os.environ.get("SEED_USER_PASSWORD", "dev-user-password-change-me")


def _offline_settings():
    """Return the deployed settings pinned to the offline model for the deterministic run."""
    base = get_settings()
    return base.model_copy(
        update={"agent_provider": "offline", "agent_model": "offline-deterministic-v1"}
    )


def _has_published_report(session, topic_id: int) -> Report | None:
    """Return the topic's most recent `published` report, or `None`.

    This is the idempotency gate: a topic that already reached `published` has nothing left to
    demonstrate, and driving the research → publish loop again would mint a new agent run and
    report for the same window.
    """
    return session.scalar(
        select(Report)
        .where(Report.topic_id == topic_id, Report.status == "published")
        .order_by(Report.id.desc())
        .limit(1)
    )


def main() -> int:
    """Seed, then drive the research → publish loop to one published report.

    Idempotent: if the topic already has a `published` report, the pass reports it and returns
    without minting a new agent run, draft, or report. Re-running against a healthy deployment
    advances nothing — the same guarantee the seeder gives.
    """
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    offline = _offline_settings()
    try:
        with factory() as session:
            # --- seed ---------------------------------------------------------
            seed._ensure_accounts(  # noqa: SLF001
                session,
                admin_password=_ADMIN_PASSWORD,
                user_password=_USER_PASSWORD,
            )
            admin = get_user_by_username(session, "admin")
            assert admin is not None
            source = seed._ensure_source(session)  # noqa: SLF001
            topic = seed._ensure_topic(session, admin)  # noqa: SLF001
            seed._ensure_repositories(session, source, topic)  # noqa: SLF001
            seed._ensure_push_targets(session)  # noqa: SLF001

            # --- idempotency gate -------------------------------------------
            published = _has_published_report(session, topic.id)
            if published is not None:
                print(
                    f"idempotent: topic {topic.id} already has a published report "
                    f"(report={published.id}); nothing to do"
                )
                return 0

            # --- research (offline) ------------------------------------------
            result = run_research_agent(session, topic_id=topic.id, settings=offline)
            print(f"research: agent_run={result.agent_run_id} status={result.status}")
            if result.agent_run_id is None:
                print(f"research produced no run: {result.to_dict()}")
                return 1

            # --- generate ----------------------------------------------------
            report = session.scalar(
                select(Report).where(Report.agent_run_id == result.agent_run_id)
            )
            if report is None:
                generated = generate_report(session, agent_run_id=result.agent_run_id)
                report = session.get(Report, generated.report_id)
            assert report is not None
            print(
                f"generate: report={report.id} version={report.current_version} "
                f"status={report.status}"
            )

            # --- verify → approve → publish → release ------------------------
            if report.status in {"draft", "pending_review", "scheduled"}:
                publish_or_request_review(session, report_id=report.id, settings=offline)
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"publish policy: status={report.status}")

            if report.status == "pending_review":
                record_review(
                    session,
                    report_id=report.id,
                    decision="approve",
                    reviewer_id=admin.id,
                    comment="演示数据：一键初始化自动审核通过",
                )
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"review: status={report.status}")

            if report.status == "approved":
                publish_or_request_review(session, report_id=report.id, settings=offline)
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"queue: status={report.status}")

            if report.status == "scheduled":
                # `AUTO_WITH_DELAY` parks the report behind a withdrawal window. The demo has
                # to show the terminal state, so release it with a clock advanced past the
                # window — the same `now` injection the publishing tests use.
                released = publish_due_reports(
                    session,
                    now=utc_now() + timedelta(hours=2),
                    limit=10,
                )
                session.expire_all()
                report = session.get(Report, report.id)
                assert report is not None
                print(f"release: released={len(released)} status={report.status}")

            print(
                f"final: report={report.id} status={report.status} version={report.current_version}"
            )
            return 0 if report.status == "published" else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
