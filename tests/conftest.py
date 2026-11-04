"""整个测试套件共享的内存数据库夹具。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_session_factory, build_settings

from open_source_intelligence.evaluation import runner as evaluation_runner
from open_source_intelligence.notifications import service as notifications_service
from open_source_intelligence.reports import publishing, verification


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """提供一个共享的内存数据库工厂。"""
    return build_session_factory()


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """提供一个绑定到共享数据库的会话。"""
    with session_factory() as active:
        yield active


@pytest.fixture()
def workspace(session: Session) -> Workspace:
    """提供绑定到当前活动会话的种子数据辅助对象。"""
    return Workspace(session)


@pytest.fixture(autouse=True)
def publish_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """在不依赖真实 `.env` 的前提下，给发布链路一套确定的 `Settings`。

    `verification`、`publishing`、推送服务与评估执行器在没有显式传入
    配置时都会回退到 `get_settings()`，这在生产里是正确的。测试在这里
    把环境限定住，既让真实的回退路径照常被覆盖，
    又不依赖开发者本机的 `.env`。显式传入配置的写法仍然有效，
    并且优先于这里的替身。
    """
    settings = build_settings()
    monkeypatch.setattr(verification, "get_settings", lambda: settings)
    monkeypatch.setattr(publishing, "get_settings", lambda: settings)
    monkeypatch.setattr(notifications_service, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation_runner, "get_settings", lambda: settings)
