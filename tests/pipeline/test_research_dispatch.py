"""守护每日批次该启动哪些研究运行的选择与派发。"""

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
# 业务日在 Asia/Shanghai 时区从 2026-10-11T16:00Z 开始。
TODAY = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)
DAY_BEFORE = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)


def build_session_factory() -> sessionmaker[Session]:
    """构造派发测试共用的内存数据库。"""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_topic(session: Session, *, name: str, enabled: bool = True) -> Topic:
    """插入一个由测试用户拥有的主题。"""
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
    """为某个主题插入一条排名运行记录。"""
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
    """插入一次研究运行，并顺带建出它需要的配置与流水线运行行。"""
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
    """插入一份与指定 Agent 运行绑定的已发布报告。"""
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
    """用用例固定的业务日期与时区调用选择器。"""
    return select_research_targets(
        session,
        business_date=BUSINESS_DATE,
        timezone_name=TIMEZONE,
        limit=limit,
    )


def test_empty_ledger_selects_nothing() -> None:
    """当天没有任何主题时应报告可运行数为零，而不是凭空造一个目标。"""
    with build_session_factory()() as session:
        selection = select_targets(session)

    assert selection.targets == []
    assert selection.eligible_count == 0
    assert selection.deferred_count == 0


def test_disabled_topic_is_not_selected() -> None:
    """停用的主题产不出候选，也就不值得为它跑一次研究。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="paused", enabled=False)
        add_rank_run(session, topic_id=topic.id, candidate_count=5)

        selection = select_targets(session)

    assert selection.targets == []


def test_topic_without_candidates_is_not_selected() -> None:
    """给排名一无所获的主题做研究，只是花预算去确认这件事。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="quiet")
        add_rank_run(session, topic_id=topic.id, candidate_count=0)

        selection = select_targets(session)

    assert selection.targets == []


def test_failed_rank_run_is_not_selected() -> None:
    """失败了的排名，其候选数不可信。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="broken")
        add_rank_run(session, topic_id=topic.id, candidate_count=5, status="failed")

        selection = select_targets(session)

    assert selection.targets == []


def test_todays_rank_run_is_not_trusted_yesterday() -> None:
    """只认当天的排名：昨天的候选可能已经过期。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="stale")
        add_rank_run(session, topic_id=topic.id, candidate_count=5, started_at=YESTERDAY)

        selection = select_targets(session)

    assert selection.targets == []


def test_topic_without_published_history_has_no_window_start() -> None:
    """从未发布过的主题退回滚动窗口（`None`）。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="fresh")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)

        selection = select_targets(session)

    assert selection.targets == [
        ResearchTarget(topic_id=topic.id, candidate_count=7, window_start=None)
    ]


def test_topic_with_published_history_carries_its_window_end() -> None:
    """主题下一轮的窗口，从上一次已发布报告的窗口终点开始。

    这是重复投递缺陷的回归守卫：相隔一天的两份报告此前共用同一个滚动的
    7 天窗口，于是重投的运行会把同样的快照再覆盖一遍。下一轮必须从上一次
    报告观测到的窗口终点起步。
    """
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        # 一份前一天的已发布报告，其运行观测到 TODAY（即它的窗口终点）。
        previous_run = add_agent_run(session, topic_id=topic.id, started_at=YESTERDAY)
        add_published_report(
            session, topic_id=topic.id, agent_run=previous_run, published_at=YESTERDAY
        )

        selection = select_targets(session)

    assert selection.targets == [
        ResearchTarget(topic_id=topic.id, candidate_count=7, window_start=YESTERDAY)
    ]


def test_window_start_uses_the_newest_published_report() -> None:
    """有多份报告时，由最新那份的窗口终点决定起点。"""
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
    """顺利路径也要带上「为什么选它」的证据，即候选数。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)

        selection = select_targets(session)

    assert selection.targets == [ResearchTarget(topic_id=topic.id, candidate_count=7)]
    assert selection.eligible_count == 1


def test_topic_already_researched_today_is_not_selected() -> None:
    """预算守卫：重投的任务不能在同一个主题上重复计费。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY)

        selection = select_targets(session)

    assert selection.targets == []
    assert selection.already_started_topic_ids == [topic.id]


def test_research_run_from_yesterday_does_not_block_today() -> None:
    """分界线是业务日，而不是「以前研究过没有」。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=YESTERDAY)

        selection = select_targets(session)

    assert [target.topic_id for target in selection.targets] == [topic.id]
    assert selection.already_started_topic_ids == []


def test_a_failed_run_today_can_be_retried() -> None:
    """失败的运行没有产出草稿，就不该占掉当天唯一一次机会。

    这不是边角情况 —— 接上派发功能后的第一次真实运行，就因为容器没有出网而以
    `AGENT_MODEL_UNAVAILABLE` 失败，而旧规则（只要有一行就算）随后一整天都报
    「已经研究过」。一次传输抖动，就足以把一个无人值守的夜晚变成空白的一天。
    """
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY, status="failed")

        selection = select_targets(session)

    assert [target.topic_id for target in selection.targets] == [topic.id]
    assert selection.already_started_topic_ids == []


def test_a_running_research_run_blocks_a_second_dispatch() -> None:
    """尚在运行的那次已经花掉预算，再派发一次就是重复消耗。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="active")
        add_rank_run(session, topic_id=topic.id, candidate_count=7)
        add_agent_run(session, topic_id=topic.id, started_at=TODAY, status="running")

        selection = select_targets(session)

    assert selection.targets == []
    assert selection.already_started_topic_ids == [topic.id]


def test_limit_truncates_and_counts_what_was_deferred() -> None:
    """有上限的批次必须报告自己没轮到的合格主题有几个。"""
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
    """重跑会取代先前那次排名，由最新的候选数说了算。"""
    with build_session_factory()() as session:
        topic = add_topic(session, name="rerun")
        add_rank_run(session, topic_id=topic.id, candidate_count=9)
        add_rank_run(session, topic_id=topic.id, candidate_count=0)

        selection = select_targets(session)

    assert selection.targets == []


def test_dispatch_records_the_task_id_per_topic() -> None:
    """报告必须把每个主题映射到传输层交回的任务标识。"""
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
    """有发布历史的主题，必须把窗口起点透传给派发函数。"""
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
    """一次入队失败不该让剩下的主题都没人研究。"""

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
    """JSON 对象装不下整数键，转换必须在序列化之前完成。"""
    report = dispatch_research_targets(
        [ResearchTarget(topic_id=9, candidate_count=1)], lambda t: "t"
    )

    payload = report.to_dict()

    assert payload["dispatched"] == {"9": "t"}
