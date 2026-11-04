"""守护 `PipelineRun` 的生命周期、错误脱敏与运行历史查询。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.collectors.types import SourceUnavailableError
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.pipeline.runs import (
    ERROR_COLLECTOR_FAILED,
    TERMINAL_STATUSES,
    classify_error,
    fail_pipeline_run,
    finish_pipeline_run,
    get_pipeline_run,
    list_pipeline_runs,
    mark_pipeline_run_running,
    start_pipeline_run,
    summarize_pipeline_run,
)


def build_session_factory() -> sessionmaker[Session]:
    """构造运行生命周期测试共用的内存数据库。"""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_topic(session: Session, *, name: str = "AI Agent") -> Topic:
    """插入一个由测试用户拥有的、已启用的主题。"""
    session.add(
        User(
            id=1,
            username="admin",
            password_hash="hash",
            display_name="Admin",
        )
    )
    topic = Topic(
        name=name,
        min_stars=0,
        window_days=7,
        max_candidates=10,
        publish_mode="AUTO_WITH_DELAY",
        enabled=True,
        created_by=1,
    )
    session.add(topic)
    session.commit()
    return topic


def test_run_lifecycle_records_status_and_metrics() -> None:
    """一次运行必须从 `queued` 经 `running` 走到终态，且每一步都留下时间戳。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        run = start_pipeline_run(session, run_type="collect", attempt=2)

        assert run.status == "queued"
        assert run.attempt == 2
        assert run.started_at is None
        assert run.finished_at is None

        mark_pipeline_run_running(session, run)
        assert run.status == "running"
        assert run.started_at is not None

        finish_pipeline_run(session, run, status="succeeded", metrics_json={"count": 3})
        summary = summarize_pipeline_run(run)

        assert summary.status == "succeeded"
        assert summary.metrics == {"count": 3}
        assert summary.attempt == 2
        assert summary.error_code is None
        assert summary.finished_at is not None
        assert summary.started_at is not None
        assert summary.finished_at >= summary.started_at
        assert datetime.fromisoformat(summary.finished_at).tzinfo is not None


def test_pinned_clock_closes_the_run_on_the_same_instant() -> None:
    """调用方钉住时钟时，运行时间戳仍要满足先后顺序的 CHECK 约束。"""
    pinned = datetime(2099, 6, 5, 12, 0, tzinfo=UTC)
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session)

        succeeded = start_pipeline_run(session, run_type="publish", topic_id=topic.id, attempt=1)
        mark_pipeline_run_running(session, succeeded, started_at=pinned)
        finish_pipeline_run(session, succeeded, status="succeeded", finished_at=pinned)
        assert succeeded.started_at == pinned
        assert succeeded.finished_at == pinned

        failed = start_pipeline_run(session, run_type="publish", topic_id=topic.id, attempt=1)
        mark_pipeline_run_running(session, failed, started_at=pinned)
        fail_pipeline_run(
            session,
            failed,
            error_code=ERROR_COLLECTOR_FAILED,
            error_message="blocked",
            finished_at=pinned,
        )
        assert failed.finished_at == pinned

        # 不钉时钟时，运行仍按墙钟时间收尾。
        default_clock = start_pipeline_run(session, run_type="snapshot", attempt=1)
        mark_pipeline_run_running(session, default_clock)
        finish_pipeline_run(session, default_clock, status="succeeded")
        assert default_clock.finished_at is not None
        assert default_clock.finished_at >= default_clock.started_at


def test_failed_run_rejects_invalid_transition_and_keeps_error() -> None:
    """已经结束的运行不能重启，错误码则被原样保留。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        run = start_pipeline_run(session, run_type="snapshot")
        mark_pipeline_run_running(session, run)
        fail_pipeline_run(
            session,
            run,
            error_code=ERROR_COLLECTOR_FAILED,
            error_message="source returned HTTP 500",
        )

        assert run.status == "failed"
        assert run.error_code == ERROR_COLLECTOR_FAILED
        assert run.error_message == "source returned HTTP 500"

        with pytest.raises(ValueError, match="already finished"):
            mark_pipeline_run_running(session, run)


def test_error_message_is_redacted_and_truncated() -> None:
    """像凭据的值绝不能原样写进运行记录。"""
    session_factory = build_session_factory()
    long_tail = "x" * 5000
    with session_factory() as session:
        run = start_pipeline_run(session, run_type="collect")
        fail_pipeline_run(
            session,
            run,
            error_code=ERROR_COLLECTOR_FAILED,
            error_message=f"failed with token=supersecretvalue {long_tail}",
        )
        stored = run.error_message or ""

    assert "supersecretvalue" not in stored
    assert "token=[REDACTED]" in stored
    assert len(stored) <= 2000


def test_topic_scope_is_enforced() -> None:
    """`agent` 与 `publish` 类型的运行必须归属某个主题。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        for run_type in ("agent", "publish"):
            with pytest.raises(ValueError, match="requires a topic_id"):
                start_pipeline_run(session, run_type=run_type)

        topic = add_topic(session)
        run = start_pipeline_run(session, run_type="agent", topic_id=topic.id)
        assert run.topic_id == topic.id
        assert run.run_type == "agent"


def test_invalid_run_type_and_status_are_rejected() -> None:
    """不支持的枚举值要在撞上数据库约束之前就失败。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        with pytest.raises(ValueError, match="unsupported run_type"):
            start_pipeline_run(session, run_type="unknown")

        run = start_pipeline_run(session, run_type="rank")
        with pytest.raises(ValueError, match="unsupported status"):
            finish_pipeline_run(session, run, status="done")
        with pytest.raises(ValueError, match="not a terminal status"):
            finish_pipeline_run(session, run, status="running")


def test_attempt_must_be_positive() -> None:
    """重试计数从 1 开始，与数据库约束保持一致。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        with pytest.raises(ValueError, match="attempt must be at least 1"):
            start_pipeline_run(session, run_type="collect", attempt=0)


def test_list_runs_filters_and_orders_newest_first() -> None:
    """运行历史必须能按类型、状态与主题筛选。"""
    session_factory = build_session_factory()
    with session_factory() as session:
        topic = add_topic(session)
        first = start_pipeline_run(session, run_type="collect")
        mark_pipeline_run_running(session, first)
        finish_pipeline_run(session, first, status="succeeded")
        second = start_pipeline_run(session, run_type="snapshot")
        fail_pipeline_run(session, second, error_code=ERROR_COLLECTOR_FAILED, error_message="boom")
        third = start_pipeline_run(session, run_type="rank", topic_id=topic.id)
        mark_pipeline_run_running(session, third)
        finish_pipeline_run(session, third, status="succeeded")

        all_runs = list_pipeline_runs(session)
        failed_runs = list_pipeline_runs(session, status="failed")
        rank_runs = list_pipeline_runs(session, run_type="rank")
        topic_runs = list_pipeline_runs(session, topic_id=topic.id)
        limited = list_pipeline_runs(session, limit=1)
        loaded = get_pipeline_run(session, second.id)

    assert [run.id for run in all_runs] == [third.id, second.id, first.id]
    assert [run.id for run in failed_runs] == [second.id]
    assert [run.id for run in rank_runs] == [third.id]
    assert [run.id for run in topic_runs] == [third.id]
    assert [run.id for run in limited] == [third.id]
    assert loaded is not None and loaded.id == second.id
    assert set(TERMINAL_STATUSES) == {"succeeded", "failed", "partial"}


def test_classify_error_maps_collector_failures() -> None:
    """采集错误不论发生在哪个阶段，都保留它专属的错误码。"""
    assert classify_error("rank", SourceUnavailableError("boom")) == ERROR_COLLECTOR_FAILED
    assert classify_error("snapshot", RuntimeError("boom")) == "SNAPSHOT_FAILED"
