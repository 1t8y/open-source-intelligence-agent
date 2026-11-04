"""校验器与发布测试共用的夹具。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy.orm import Session
from support import Workspace


@pytest.fixture
def report_case(session: Session, workspace: Workspace) -> Iterator[SeededReport]:
    """提供一个已生成好的报告，其主题使用 `AUTO_WITH_DELAY`。"""
    yield build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
