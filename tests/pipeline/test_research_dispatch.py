"""Tests for choosing and dispatching the research runs a daily batch should start."""

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.agent import AgentConfig, AgentRun
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import PipelineRun, Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.pipeline.research_dispatch import (
    ResearchTarget,
    dispatch_research_targets,
    select_research_targets,
)

BUSINESS_DATE = date(2026, 10, 12)
TIMEZONE = "Asia/Shanghai"
# The business day starts at 2026-10-11T16:00Z in Asia/Shanghai.
TODAY = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)
DAY_BEFORE = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)


def build_session_factory() -> sessionmaker[Session]:
    """Build a shared in-memory database for dispatch tests."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_topic(session: Session, *, name: str, enabled: bool = True) -> Topic:
    """Insert one topic owned by a test user."""
    user = session.scalar(select(User).where(User.id == 1))
    if user is None:
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
        enabled=enabled,
        created_by=1,
    )
    session.add(topic)
    session.commit()
    return topic


def add_rank_run(
    session: Session,
    *,
    topic_id: int,
    candidate_count: int,
    started_at: datetime = TODAY,
    status: str = "succeeded",
) -> PipelineRun:
    """Insert one rank run for a topic."""
    run = PipelineRun(
        topic_id=topic_id,
        run_type="rank",
        status=status,
        started_at=started_at,
        finished_at=started_at,
        attempt=1,
        metrics_json={"candidate_count": candidate_count},
    )
    session.add(run)
    session.commit()
    return run


def add_agent_run(
    session: Session,
    *,
    topic_id: int,
    started_at: datetime = TODAY,
    status: str = "completed",
) -> AgentRun:
    """Insert one research run, creating the config and pipeline run it requires."""
    config = session.scalar(select(AgentConfig).limit(1))
    if config is None:
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
        started_at=started_at,
        finished_at=started_at,
        attempt=1,
    )
    session.add(pipeline_run)
    session.flush()
    agent_run = AgentRun(
        pipeline_run_id=pipeline_run.id,
        topic_id=topic_id,
        agent_config_id=config.id,
        status=status,
        window_start=YESTERDAY,
        window_end=started_at,
        started_at=started_at,
    )
    session.add(agent_run)
    session.commit()
    return agent_run


def add_published_report(
    session: Session,
    *,
    topic_id: int,
    agent_run: AgentRun,
    published_at: datetime = TODAY,
) -> Report:
    """Insert one published report tied to the given agent run."""
    report = Report(
        topic_id=topic_id,
        agent_run_id=agent_run.id,
        title="weekly",
        content_markdown="body",
        status="published",
        sensitivity="PUBLIC",
        visibility="TEAM",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=published_at,
        published_at=published_at,
    )
    session.add(report)
    session.commit()
    return report


def select_targets(session: Session, *, limit: int = 5):
    """Call the selector with the test's fixed business date and timezone."""
    return select_research_targets(
        session,
        business_date=BUSINESS_DATE,
        timezone_name=TIMEZONE,
        limit=limit,
    )


def test_empty_ledger_selects_nothing() -> None:
    """A day with no topics must report zero eligible, not a synthetic target."""
    with build_session_factory()() as session:
        selection = select_targets(session)

    assert selection.targets == []
    assert selection.eligible_count == 0
    assert selection.deferred_count == 0


def test_disabled_topic_is_not_selected() -> None:
    """A disabled topic produces no candidates, so it is not worth a research run."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="paused", enabled=False)
        add_rank_run(session, topic_id=topic.id, candidate_count=5)

        selection = select_targets(session)

    assert selection.targets == []


def test_topic_without_candidates_is_not_selected() -> None:
    """Researching a topic whose ranking found nothing only spends budget to learn that."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="quiet")
        add_rank_run(session, topic_id=topic.id, candidate_count=0)

        selection = select_targets(session)

    assert selection.targets == []


def test_failed_rank_run_is_not_selected() -> None:
    """A ranking that failed has no trustworthy candidate count."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="broken")
        add_rank_run(session, topic_id=topic.id, candidate_count=5, status="failed")

        selection = select_targets(session)

    assert selection.targets == []


def test_todays_rank_run_is_not_trusted_yesterday() -> None:
    """Only today's ranking counts: yesterday's candidates may already be stale."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="stale")
        add_rank_run(session, topic_id=topic.id, candidate_count=5, started_at=YESTERDAY)

        selection = select_targets(session)

    assert selection.targets == []


def test_topic_without_published_history_has_no_window_start() -> None:
    """A topic that has never published falls back to the trailing window (None)."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="fresh")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)

        selection = select_targets(session)

    assert selection.targets == [
        ResearchTarget(topic_id=topic.id, candidate_count=7, window_start=None)
    ]


def test_topic_with_published_history_carries_its_window_end() -> None:
    """A topic's next window starts where its last published report's window ended.

    This is the regression guard for the duplicate-delivery bug: two reports a day apart used to
    share a sliding 7-day window, so a re-delivered run re-covered the same snapshots. The next
    run must begin at the previous report's observed window end.
    """
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        # A published report from a previous day, whose run observed up to TODAY (its window end).
        previous_run = add_agent_run(session, topic_id=topic.id, started_at=YESTERDAY)
        add_published_report(
            session, topic_id=topic.id, agent_run=previous_run, published_at=YESTERDAY
        )

        selection = select_targets(session)

    assert selection.targets == [
        ResearchTarget(topic_id=topic.id, candidate_count=7, window_start=YESTERDAY)
    ]


def test_window_start_uses_the_newest_published_report() -> None:
    """When several reports exist, the most recent one's window end decides the start."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        older_run = add_agent_run(session, topic_id=topic.id, started_at=DAY_BEFORE)
        add_published_report(
            session, topic_id=topic.id, agent_run=older_run, published_at=DAY_BEFORE
        )
        newer_run = add_agent_run(session, topic_id=topic.id, started_at=YESTERDAY)
        add_published_report(
            session, topic_id=topic.id, agent_run=newer_run, published_at=YESTERDAY
        )

        selection = select_targets(session)

    assert selection.targets == [
        ResearchTarget(topic_id=topic.id, candidate_count=7, window_start=YESTERDAY)
    ]


def test_eligible_topic_is_selected_with_its_candidate_count() -> None:
    """The happy path carries the evidence for why the topic was chosen."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)

        selection = select_targets(session)

    assert selection.targets == [ResearchTarget(topic_id=topic.id, candidate_count=7)]
    assert selection.eligible_count == 1


def test_topic_already_researched_today_is_not_selected() -> None:
    """The budget guard: a re-delivered task must not charge for the same topic twice."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY)

        selection = select_targets(session)

    assert selection.targets == []
    assert selection.already_started_topic_ids == [topic.id]


def test_research_run_from_yesterday_does_not_block_today() -> None:
    """The boundary is the business day, not "ever researched before"."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=YESTERDAY)

        selection = select_targets(session)

    assert [target.topic_id for target in selection.targets] == [topic.id]
    assert selection.already_started_topic_ids == []


def test_a_failed_run_today_can_be_retried() -> None:
    """A run that failed produced no draft: it must not spend the day's only attempt.

    This is not a corner case - the first real run after wiring the dispatch failed with
    `AGENT_MODEL_UNAVAILABLE` because the container had no outbound network, and the old rule
    (any row counts) then reported "already researched" for the rest of the day. One transport
    blip would have turned an unattended night into an empty day.
    """
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY, status="failed")

        selection = select_targets(session)

    assert [target.topic_id for target in selection.targets] == [topic.id]
    assert selection.already_started_topic_ids == []


def test_a_running_research_run_blocks_a_second_dispatch() -> None:
    """A run in flight has spent budget already; dispatching again would duplicate it."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY, status="running")

        selection = select_targets(session)

    assert selection.targets == []
    assert selection.already_started_topic_ids == [topic.id]


def test_limit_truncates_and_counts_what_was_deferred() -> None:
    """A bounded batch must report how many eligible topics it did not reach."""
    with build_session_factory()() as session:
        first = add_topic(session, name="first")
        second = add_topic(session, name="second")
        add_rank_run(session, topic_id=first.id, candidate_count=3)
        add_rank_run(session, topic_id=second.id, candidate_count=4)

        selection = select_targets(session, limit=1)

    assert [target.topic_id for target in selection.targets] == [first.id]
    assert selection.eligible_count == 2
    assert selection.deferred_count == 1


def test_newest_successful_rank_run_wins_over_an_older_one() -> None:
    """A rerun supersedes the earlier ranking: the freshest count is the one that decides."""
    with build_session_factory()() as session:
        topic = add_topic(session, name="rerun")
        add_rank_run(session, topic_id=topic.id, candidate_count=9)
        add_rank_run(session, topic_id=topic.id, candidate_count=0)

        selection = select_targets(session)

    assert selection.targets == []


def test_dispatch_records_the_task_id_per_topic() -> None:
    """The report must map each topic to what the transport handed back."""
    dispatched: list[int] = []

    def dispatcher(target: ResearchTarget) -> str:
        dispatched.append(target.topic_id)
        return f"task-{target.topic_id}"

    report = dispatch_research_targets(
        [
            ResearchTarget(topic_id=3, candidate_count=1),
            ResearchTarget(topic_id=4, candidate_count=2),
        ],
        dispatcher,
    )

    assert dispatched == [3, 4]
    assert report.dispatched == {3: "task-3", 4: "task-4"}
    assert report.errors == {}


def test_dispatcher_receives_the_window_start() -> None:
    """A topic with published history must hand its window start through to the dispatcher."""
    seen: list[datetime | None] = []

    def dispatcher(target: ResearchTarget) -> str:
        seen.append(target.window_start)
        return "t"

    report = dispatch_research_targets(
        [ResearchTarget(topic_id=9, candidate_count=1, window_start=TODAY)],
        dispatcher,
    )

    assert seen == [TODAY]
    assert report.dispatched == {9: "t"}


def test_one_topic_failing_to_enqueue_does_not_stop_the_others() -> None:
    """One broken enqueue must not leave the remaining topics unresearched."""

    def dispatcher(target: ResearchTarget) -> str:
        if target.topic_id == 3:
            raise RuntimeError("broker unavailable")
        return f"task-{target.topic_id}"

    report = dispatch_research_targets(
        [
            ResearchTarget(topic_id=3, candidate_count=1),
            ResearchTarget(topic_id=4, candidate_count=2),
        ],
        dispatcher,
    )

    assert report.dispatched == {4: "task-4"}
    assert list(report.errors) == [3]
    assert "broker unavailable" in report.errors[3]


def test_dispatch_payload_uses_string_keys() -> None:
    """A JSON object cannot carry integer keys; the conversion must happen before serialising."""
    report = dispatch_research_targets(
        [ResearchTarget(topic_id=9, candidate_count=1)], lambda t: "t"
    )

    payload = report.to_dict()

    assert payload["dispatched"] == {"9": "t"}
