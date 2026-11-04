"""守护每日任务里的研究派发步骤。"""

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
    """为派发用例构造一套确定的 Worker 配置。"""
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
    """为派发用例构造一个共享的内存数据库。"""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_ready_topic(session: Session, *, name: str, candidates: int = 4) -> Topic:
    """插入一个启用主题，并给它当天的排名运行造一条成功记录。"""
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
    """插入一份已发布报告，使它背后的运行观测到 `window_end` 为止。"""
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
    """Celery 应用替身，记录它被要求发送的任务。"""

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
    """证明这一步只做一件事：把今天合格的主题变成排队中的研究任务。"""
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
    """证明已发布过报告的主题，要把下一段窗口起点交给排队中的任务。

    这是重复投递修复的 Worker 侧一半：`select_research_targets` 算出的窗口起点
    必须送到 Celery 任务手里，否则研究运行会悄悄退回完整的回溯窗口，
    把同一批快照再覆盖一遍。
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
    """证明拒收任务的 broker 是一个要上报的结果，而不是要抛出去的异常。"""
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
    """证明研究任务不进 beat 表：它由每日批次派发，而不是由时钟触发。

    同一类运行配两套调度，一旦两边都触发，当天的花费就会翻倍。
    """
    settings = build_settings(auto_research=True)

    schedule = build_beat_schedule(settings)

    assert "daily-deterministic-pipeline" in schedule
    assert all(entry["task"] != RESEARCH_AGENT_TASK for entry in schedule.values())


def test_the_limit_bounds_the_batch_and_reports_the_remainder() -> None:
    """证明 `AGENT_DAILY_RESEARCH_LIMIT` 是真正的上限，剩下的必须留得可见。"""
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
