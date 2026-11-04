"""守护运维配置面：谁看得到，谁只会被告知「去问别人」。

这条规则背后放着两样东西。**推送目标**属于部署配置 —— 它决定了已发布的报告可以
从哪些目的地出门；**账号**则由引导 CLI 创建。两者都不由控制台管理，归后端所有。
所以列出白名单的页面只画给管理员看，因为他能对它动手；而那种只需要说出「你还不能
推送」的页面，措辞要随读者是否能行动而不同。

另一半是：渲染规则不等于闸门。把按钮从某个角色眼前藏起来，并不能阻止他去 POST
它，所以这里每一处都要查两遍：页面画了什么，路由又答了什么。只查前者的测试会在
一个说谎的界面上通过；只查后者的测试，则会在一份「放着一个谁都没权限用的按钮」的
界面上通过。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

#: 同时承载投递流水与白名单卡片的那一页。
PUSHES = "/ui/pushes"
REPORTS = "/ui/reports"
RELEASE_ACTION = 'action="/ui/reports/publish-due"'

#: 白名单所在的那张表。这是后端对它的叫法，一个没法对它动手的读者就不该拿到它：
#: 一页告诉编辑去改 `push_targets`，等于叫他们做一件根本做不到的事。
ALLOWLIST_TABLE = "push_targets"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def actors(session: Session) -> dict[str, Actor]:
    """每个标准角色一个账号，都落在团队数据范围上。"""
    return {
        role: create_actor(session, username=f"ops-{role.lower()}", role=role, data_scope="team")
        for role in ("ADMIN", "EDITOR", "VIEWER")
    }


# --------------------------------------------------------------------------------------
# 白名单卡片
# --------------------------------------------------------------------------------------


def test_an_admin_reads_the_allowlist_on_the_delivery_page(
    workspace: Workspace,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """卡片展示整份白名单，包括那两个会放宽投递范围的开关。"""
    target = workspace.add_push_target(name="ops-email")

    assert web_login(client, actors["ADMIN"]).status_code == 303
    page = client.get(PUSHES)

    assert page.status_code == 200
    assert "推送目标白名单" in page.text
    assert "允许机密" in page.text
    assert target.name in page.text


def test_an_editor_gets_the_delivery_ledger_without_the_allowlist(
    workspace: Workspace,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """编辑能读投递了什么，但读不到决定它能去哪的那份配置。

    流水回答的是「我推的那份报告到了吗」，白名单回答的是「这个平台可以往外发
    什么、在哪些开关之下」。这是两个不同的问题，只有前者是编辑该管的 —— 所以
    卡片、它的列、它的目的地，以及后端给那张表起的名字，全都不出现。
    """
    target = workspace.add_push_target(name="ops-email")

    assert web_login(client, actors["EDITOR"]).status_code == 303
    page = client.get(PUSHES)

    assert page.status_code == 200
    assert "投递记录" in page.text
    assert "推送目标白名单" not in page.text
    assert "允许机密" not in page.text
    assert ALLOWLIST_TABLE not in page.text
    assert target.name not in page.text


def test_a_viewer_cannot_open_the_delivery_page_at_all(
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """卡片的可见性是第二道门；这一页自己的那道门没有变。"""
    assert web_login(client, actors["VIEWER"]).status_code == 303
    assert client.get(PUSHES).status_code == 403


# --------------------------------------------------------------------------------------
# 报告列表上的放行按钮
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("role", "drawn"), [("ADMIN", True), ("EDITOR", True), ("VIEWER", False)])
def test_the_release_driver_is_drawn_only_for_a_role_that_can_run_it(
    role: str,
    drawn: bool,
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """按钮跟随路由自己的角色门 —— 它曾经连读者也一并给了。

    只读用户当时看得见按钮，点下去才知道路由会拒绝。这和「侧边栏入口点开是
    403」是同一个缺陷，也用同一个修法：按端点真正执行的规则来渲染这个控件。
    """
    assert web_login(client, actors[role]).status_code == 303
    page = client.get(REPORTS)

    assert page.status_code == 200
    assert (RELEASE_ACTION in page.text) is drawn


def test_hiding_the_button_is_not_what_stops_a_read_only_role(
    actors: dict[str, Actor],
    client: TestClient,
) -> None:
    """按钮缺席只是礼貌，不是闸门：那个 POST 自己就会被拒。

    两半都需要。少了路由这一道，界面的规则就成了唯一的规则，一次渲染改动便会悄悄
    变成一次授权改动。
    """
    assert web_login(client, actors["VIEWER"]).status_code == 303
    client.get(REPORTS)

    response = client.post("/ui/reports/publish-due", data={"csrf_token": csrf_value(client)})

    assert response.status_code == 403
