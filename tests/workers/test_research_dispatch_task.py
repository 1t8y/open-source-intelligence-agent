"""Tests for the daily task's research dispatch step."""

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.workers.celery_app import RESEARCH_AGENT_TASK, build_beat_schedule
from open_source_intelligence.workers.tasks import _dispatch_research

BUSINESS_DATE = date(2026, 10, 12)
TODAY = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)


def build_settings(*, auto_research: bool, limit: int = 2) -> Settings:
    """Create deterministic worker settings for dispatch tests."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        app_timezone="Asia/Shanghai",
        agent_auto_research_enabled=auto_research,
        agent_daily_research_limit=limit,
    )


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database for dispatch tests."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_ready_topic(session: Session, *, name: str, candidates: int = 4) -> Topic:
    """Insert an enabled topic with a successful rank run for the business date."""
    if session.scalar(select(User).where(User.id == 1)) is None:
        session.add(User(id=1, username="admin", password_hash="hash", display_name="Admin"))
        session.flush()
    topic = Topic(
        name=name,
        languages_json=None,
        keywords_json=None,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=1,
    )
    session.add(topic)
    session.flush()
    session.add(
        PipelineRun(
            topic_id=topic.id,
            run_type="rank",
            status="succeeded",
            started_at=TODAY,
            finished_at=TODAY,
            attempt=1,
            metrics_json={"candidate_count": candidates},
        )
    )
    session.commit()
    return topic


def add_published_history(session: Session, *, topic_id: int, window_end: datetime) -> None:
    """Insert a published report whose run observed up to `window_end`."""
    config = AgentConfig(
        name="default",
        version="v1",
        provider="offline",
        model="offline-deterministic-v1",
        prompt_version="v1",
        tool_policy_json={"tools": []},
        max_loops=4,
        max_model_calls=6,
        max_tool_calls=10,
        max_tokens=30000,
        max_cost=0.5,
        enabled=True,
    )
    session.add(config)
    session.flush()
    pipeline_run = PipelineRun(
        topic_id=topic_id,
        run_type="agent",
        status="succeeded",
        started_at=window_end,
        finished_at=window_end,
        attempt=1,
    )
    session.add(pipeline_run)
    session.flush()
    agent_run = AgentRun(
        pipeline_run_id=pipeline_run.id,
        topic_id=topic_id,
        agent_config_id=config.id,
        status="completed",
        window_start=window_end,
        window_end=window_end,
        started_at=window_end,
    )
    session.add(agent_run)
    session.flush()
    session.add(
        Report(
            topic_id=topic_id,
            agent_run_id=agent_run.id,
            title="weekly",
            content_markdown="body",
            status="published",
            sensitivity="PUBLIC",
            visibility="TEAM",
            publish_mode="AUTO_WITH_DELAY",
            generated_at=window_end,
            published_at=window_end,
        )
    )
    session.commit()


class RecordingApp:
    """Celery-app double that records the tasks it was asked to send."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self._error = error

    def send_task(self, name: str, *, kwargs: dict[str, object]) -> object:
        if self._error is not None:
            raise self._error
        self.sent.append((name, kwargs))

        class _Result:
            id = f"task-{len(self.sent)}"

        return _Result()


def test_dispatch_queues_one_research_task_per_eligible_topic() -> None:
    """The step's only job: turn today's eligible topics into queued research tasks."""
    with build_session_factory()() as session:
        topic = add_ready_topic(session, name="active")
        app = RecordingApp()

        payload = _dispatch_research(
            session,
            business_date=BUSINESS_DATE,
            settings=build_settings(auto_research=True),
            app=app,
        )

    assert app.sent == [(RESEARCH_AGENT_TASK, {"topic_id": topic.id})]
    assert payload["dispatched"] == {str(topic.id): "task-1"}
    assert payload["eligible"] == 1
    assert payload["deferred"] == 0


def test_dispatch_passes_the_window_start_to_a_topic_with_history() -> None:
    """A topic that already published must hand its next window start to the queued task.

    This is the worker-side half of the duplicate-delivery fix: the window start computed by
    `select_research_targets` has to reach the Celery task, otherwise the research run silently
    falls back to the full trailing window and re-covers the same snapshots.
    """
    with build_session_factory()() as session:
        topic = add_ready_topic(session, name="active")
        add_published_history(session, topic_id=topic.id, window_end=YESTERDAY)
        app = RecordingApp()

        _dispatch_research(
            session,
            business_date=BUSINESS_DATE,
            settings=build_settings(auto_research=True),
            app=app,
        )

    assert app.sent == [
        (RESEARCH_AGENT_TASK, {"topic_id": topic.id, "window_start": YESTERDAY.isoformat()})
    ]


def test_dispatch_payload_reports_a_broker_failure_without_raising() -> None:
    """A broker that refuses the task is a result to report, not an exception to propagate."""
    with build_session_factory()() as session:
        add_ready_topic(session, name="active")
        app = RecordingApp(error=RuntimeError("broker unavailable"))

        payload = _dispatch_research(
            session,
            business_date=BUSINESS_DATE,
            settings=build_settings(auto_research=True),
            app=app,
        )

    assert payload["dispatched"] == {}
    assert len(payload["errors"]) == 1


def test_the_switch_does_not_add_a_beat_entry() -> None:
    """Research stays out of the beat table: it is dispatched by the daily batch, not by a clock.

    Two schedules for one run type would double the spend on a day when both fired.
    """
    settings = build_settings(auto_research=True)

    schedule = build_beat_schedule(settings)

    assert "daily-deterministic-pipeline" in schedule
    assert all(entry["task"] != RESEARCH_AGENT_TASK for entry in schedule.values())


def test_the_limit_bounds_the_batch_and_reports_the_remainder() -> None:
    """`AGENT_DAILY_RESEARCH_LIMIT` must be a real bound, and the leftovers must stay visible."""
    with build_session_factory()() as session:
        first = add_ready_topic(session, name="first")
        add_ready_topic(session, name="second")
        app = RecordingApp()

        payload = _dispatch_research(
            session,
            business_date=BUSINESS_DATE,
            settings=build_settings(auto_research=True, limit=1),
            app=app,
        )

    assert [kwargs["topic_id"] for _, kwargs in app.sent] == [first.id]
    assert payload["deferred"] == 1
