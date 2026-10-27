"""Tests for rendering report bodies: Markdown in, sanitized HTML out.

The body used to reach the page inside a `<pre>`, so a reader saw `## 重点项目` and `**加粗**`
literally — the interface was showing the file format. Rendering it is one half; the other half is
that the body quotes repository READMEs, which are third-party text, so nothing in the source may
become live markup.
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
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor, who may read every report in these fixtures."""
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
    # The syntax characters are consumed by the renderer; they must not survive as content.
    assert "## 重点项目" not in html
    assert "|---|" not in html


def test_html_in_the_source_is_escaped_not_executed() -> None:
    """A quoted README must not be able to put markup on the page."""
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
    # The raw syntax no longer leaks into the page.
    assert "## 重点项目" not in page.text
    assert "&lt;script&gt;" in page.text


def test_the_share_page_renders_it_the_same_way(
    session: Session,
    workspace: Workspace,
    editor: Actor,
    client: TestClient,
) -> None:
    """The public page must not be the one place that still shows raw Markdown."""
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
    # The shell has its own <script> tags, so the assertion is on the escaped body, not on the page.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_a_rollback_reason_carries_its_version_through_the_label() -> None:
    """`rollback_to_v3` is generated, so it is translated by rule rather than by table entry."""
    assert _label("rollback_to_v3") == "回滚到 v3"
    assert _label("generated_from_agent_draft") == "由 Agent 草稿生成"
    assert _label("approve") == "通过"
    # An identifier with no label falls through unchanged rather than becoming blank.
    assert _label("agent_no_tools") == "agent_no_tools"
