"""守护报告正文的渲染：Markdown 进来，净化后的 HTML 出去。

正文曾经被塞进 `<pre>` 里送到页面，读者看到的是 `## 重点项目` 和 `**加粗**` 的
原样 —— 界面在展示文件格式。渲染只是其中一半；另一半是正文里引用着仓库 README，
那是第三方文本，所以来源里的任何东西都不能变成活的标记。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, web_login
from fastapi.testclient import TestClient
from notifications_support import PUBLISHED, seed_pushable_report
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.web.markdown import render_markdown
from open_source_intelligence.web.router import _label

BODY = """## 重点项目

本周主线由 **morulot/rea** 拉动。

| 仓库 | 净增 |
|---|---|
| morulot/rea | 29,855 |

---

<script>alert(1)</script>
"""


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """编辑：这些夹具里的每一份报告它都读得到。"""
    return create_actor(session, username="md-editor", role="EDITOR", data_scope="team")


def test_markdown_becomes_html() -> None:
    html = str(render_markdown("**粗体** 与 `code`"))

    assert "<strong>粗体</strong>" in html
    assert "<code>code</code>" in html
    assert "**" not in html


def test_headings_tables_and_rules_are_structure_not_text() -> None:
    html = str(render_markdown(BODY))

    assert "<h2>重点项目</h2>" in html
    assert "<table>" in html
    assert "<th>仓库</th>" in html
    assert "<hr" in html
    # 语法字符被渲染器消耗掉了，不能再作为内容留在页面上。
    assert "## 重点项目" not in html
    assert "|---|" not in html


def test_html_in_the_source_is_escaped_not_executed() -> None:
    """被引用的 README 不能把任何标记放进页面。"""
    html = str(render_markdown(BODY))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_empty_body_renders_to_nothing() -> None:
    assert str(render_markdown(None)) == ""
    assert str(render_markdown("")) == ""


def test_the_report_page_renders_the_body(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    report = seed_pushable_report(session, workspace, status=PUBLISHED)
    report.content_markdown = BODY
    session.flush()
    assert web_login(client, editor).status_code == 303

    page = client.get(f"/ui/reports/{report.id}")

    assert page.status_code == 200
    assert "<h2>重点项目</h2>" in page.text
    assert "<strong>morulot/rea</strong>" in page.text
    # 原始语法不再泄漏到页面上。
    assert "## 重点项目" not in page.text
    assert "&lt;script&gt;" in page.text


def test_the_share_page_renders_it_the_same_way(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """公开页面不能成为唯一还在显示原始 Markdown 的地方。"""
    from open_source_intelligence.reports.sharing import create_share_link

    report = seed_pushable_report(session, workspace, status=PUBLISHED, sensitivity="PUBLIC")
    report.content_markdown = BODY
    session.flush()
    issued = create_share_link(session, report_id=report.id, actor_id=editor.user_id)
    assert web_login(client, editor).status_code == 303

    page = client.get(f"/share/{issued.token}")

    assert page.status_code == 200
    assert "<h2>重点项目</h2>" in page.text
    assert "## 重点项目" not in page.text
    # 页面外壳自带 `<script>` 标签，所以断言打在转义后的正文上，而不是整个页面。
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_a_rollback_reason_carries_its_version_through_the_label() -> None:
    """`rollback_to_v3` 是生成出来的，所以按规则翻译，而不是查表。"""
    assert _label("rollback_to_v3") == "回滚到 v3"
    assert _label("generated_from_agent_draft") == "由 Agent 草稿生成"
    assert _label("approve") == "通过"
    # 没有对应标签的标识符原样落下，而不是变成空白。
    assert _label("agent_no_tools") == "agent_no_tools"
