"""运维面四个页面的时间也必须和报告页同一个口径。

修报告页时只改了报告与主题这两处，于是同一个平台里出现了两套时间：报告页说「11-19
02:55 +0800」，而运维看板、运行列表、评测列表、审计日志仍然把 `18:55 UTC` 当傍晚显示。
追一条凌晨发出去的报告到底发生了什么，正好要在这四个页面之间来回跳 —— 口径不一致时，
差的这八小时会让人以为是两次不同的事件。这里按页面各钉一条：本地时间出现，UTC 原文
不出现。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models import (
    AgentStep,
    AuditLog,
    BackfillJob,
    EvalDataset,
    EvalRun,
    PipelineRun,
)

#: 18:55:54 UTC 就是次日 02:55:54（+0800）——「凌晨那封邮件」的那个时刻。
STAMP = datetime(2026, 11, 18, 18, 55, 54, tzinfo=UTC)
LOCAL_MINUTE = "2026-11-19 02:55 +0800"
LOCAL_SECOND = "2026-11-19 02:55:54 +0800"
#: 页面若退回直接 `strftime`，出现的就是这一串。
RAW_UTC_MINUTE = "2026-11-18 18:55"
RAW_UTC_SECOND = "2026-11-18 18:55:54"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """四个运维页面都要求 ADMIN / EDITOR，探针统一用管理员。"""
    return create_actor(session, username="ops-local-time", role="ADMIN", data_scope="team")


def test_the_runs_page_renders_the_start_time_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """流水线运行列表的「开始时间」列。"""
    topic = workspace.add_topic(name="ops-runs-topic")
    session.add(
        PipelineRun(
            topic_id=topic.id,
            run_type="agent",
            status="succeeded",
            attempt=1,
            started_at=STAMP,
            finished_at=STAMP,
        )
    )
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/runs")

    assert response.status_code == 200
    assert LOCAL_MINUTE in response.text
    assert RAW_UTC_MINUTE not in response.text


def test_the_backfill_section_renders_iso_strings_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """回填任务区块的时间是 ISO 字符串，过去靠切前 16 个字符显示 —— 连时区都没提。

    这条正是探针在真实页面上抓到的漏网：另外四张表都换了过滤器，唯独这里把
    `2026-10-16T14:32:00` 切成 `2026-10-16 14:32` 直接贴出去。
    """
    session.add(
        BackfillJob(
            provider="github",
            query_json={"q": "agent"},
            status="succeeded",
            target_pages=1,
            per_page=100,
            started_at=STAMP,
            heartbeat_at=STAMP,
        )
    )
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/runs")

    assert response.status_code == 200
    assert LOCAL_MINUTE in response.text
    assert RAW_UTC_MINUTE not in response.text


def test_the_audit_page_renders_entry_times_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """审计日志是追查「谁在什么时候动了这条报告」的最后一站。"""
    session.add(
        AuditLog(
            actor_id=admin.user_id,
            action="report.read",
            resource_type="report",
            resource_id="1",
            result="success",
            request_id="req-ops-local-time",
            created_at=STAMP,
        )
    )
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/audit")

    assert response.status_code == 200
    assert LOCAL_SECOND in response.text
    assert RAW_UTC_SECOND not in response.text


def test_the_evaluations_page_renders_run_times_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """数据集创建时间与评测运行的起止时间。"""
    dataset = EvalDataset(name="ops-local-time-dataset", version="v1")
    session.add(dataset)
    session.flush()
    session.add(
        EvalRun(
            dataset_id=dataset.id,
            target_type="agent_full",
            status="succeeded",
            started_at=STAMP,
            finished_at=STAMP,
            created_at=STAMP,
        )
    )
    dataset.created_at = STAMP
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/evaluations")

    assert response.status_code == 200
    assert LOCAL_MINUTE in response.text
    assert RAW_UTC_MINUTE not in response.text


def test_the_dashboard_labels_its_window_with_the_offset(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """看板的统计窗口过去明写「（UTC）」，现在两端都带本地偏移，说明也就不需要了。"""
    assert web_login(client, admin).status_code == 303
    response = client.get("/ui/dashboard")

    assert response.status_code == 200
    assert "+0800" in response.text
    assert "UTC" not in response.text


def test_the_run_trace_renders_steps_in_local_time(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """轨迹页的窗口、开始时间与每一步的时刻。"""
    topic = workspace.add_topic(name="ops-trace-topic")
    agent_run = workspace.add_agent_run(topic.id)
    agent_run.started_at = STAMP
    agent_run.window_start = STAMP
    agent_run.window_end = STAMP
    session.add(
        AgentStep(
            agent_run_id=agent_run.id,
            step_index=1,
            step_type="tool",
            status="ok",
            summary="executed 1 tool call(s)",
            latency_ms=1,
            created_at=STAMP,
        )
    )
    session.commit()

    assert web_login(client, admin).status_code == 303
    response = client.get(f"/ui/dashboard/runs/{agent_run.id}")

    assert response.status_code == 200
    assert LOCAL_SECOND in response.text
    assert RAW_UTC_SECOND not in response.text
