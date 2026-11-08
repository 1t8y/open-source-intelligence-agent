"""守护「为什么没发」这条可追溯路径。

发布策略的裁决写在 `pipeline_runs.metrics_json`（`stage=evaluate` 带 report_id 与
reason_codes）。以前这些只有「采集与运行」页能看到，而且是一串原始码，既没有中文解释，
也没有点进那份报告去审核/调整的入口。这组测试守的是三处 DOM 结构：

* 采集与运行的流水线行要给出 `/ui/reports/{id}` 链接；
* 报告详情要有一块 `#publish-reason` 说明未发布原因；
* 报告中心列表的未发布报告也要带上同一份说明。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.pipeline.runs import (
    finish_pipeline_run,
    start_pipeline_run,
)


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """采集与运行只对管理员和编辑者开放。"""
    return create_actor(session, username="reason-admin", role="ADMIN", data_scope="team")


def _decision_run(
    session: Session,
    *,
    topic_id: int,
    report_id: int,
    resolution: str,
    reasons: list[str],
) -> int:
    """写一条发布裁决运行，返回它的 id。"""
    run = start_pipeline_run(session, run_type="publish", topic_id=topic_id, attempt=1)
    metrics: dict[str, Any] = {
        "stage": "evaluate",
        "version": 1,
        "report_id": report_id,
        "publish_at": None,
        "resolution": resolution,
        "publish_mode": "AUTO_WITH_DELAY",
        "reason_codes": reasons,
        "requires_review": True,
    }
    finish_pipeline_run(
        session,
        run,
        status="succeeded",
        metrics_json=metrics,
        error_code=None,
        error_message=None,
        finished_at=NOW + timedelta(seconds=5),
    )
    return int(run.id)


def _pending_report(session: Session, workspace: Workspace) -> tuple[int, int]:
    """建一份停在待审核的报告，返回 (report_id, topic_id)。"""
    topic = workspace.add_topic()
    agent_run = workspace.add_agent_run(topic.id)
    report = workspace.add_report(
        topic_id=topic.id,
        agent_run_id=agent_run.id,
        title="PENDING-REASON",
        status="pending_review",
    )
    session.flush()
    return int(report.id), int(topic.id)


def test_runs_page_links_the_decision_to_its_report(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """从采集与运行的发布裁决行，要能一键跳进报告详情。"""
    report_id, topic_id = _pending_report(session, workspace)
    _decision_run(
        session,
        topic_id=topic_id,
        report_id=report_id,
        resolution="pending_review",
        reasons=["FIRST_RUN"],
    )
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/runs")

    assert response.status_code == 200
    assert f'href="/ui/reports/{report_id}"' in response.text


def test_report_detail_shows_the_publish_reason(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """报告详情要有一块明确的未发布原因，而不是只有一个「待审核」徽章。"""
    report_id, topic_id = _pending_report(session, workspace)
    _decision_run(
        session,
        topic_id=topic_id,
        report_id=report_id,
        resolution="pending_review",
        reasons=["FIRST_RUN", "AGENT_RUN_NOT_CLEAN"],
    )
    assert web_login(client, admin).status_code == 303

    response = client.get(f"/ui/reports/{report_id}")

    assert response.status_code == 200
    assert 'id="publish-reason"' in response.text
    # 同一页上要有真正能提交审核决定的动作，而不是只解释原因。
    assert f'action="/ui/reports/{report_id}/review"' in response.text


def test_reports_list_shows_the_reason_for_unpublished_rows(
    session: Session,
    workspace: Workspace,
    admin: Actor,
    client: TestClient,
) -> None:
    """报告中心列表对未发布报告也要给出同一份原因。"""
    report_id, topic_id = _pending_report(session, workspace)
    _decision_run(
        session,
        topic_id=topic_id,
        report_id=report_id,
        resolution="pending_review",
        reasons=["AGENT_RUN_NOT_CLEAN"],
    )
    assert web_login(client, admin).status_code == 303

    response = client.get("/ui/reports")

    assert response.status_code == 200
    assert 'class="hint publish-reason"' in response.text
    assert f'href="/ui/reports/{report_id}"' in response.text
