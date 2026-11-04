"""守护侧边栏外壳与挂在它上面的那些页面：八页共用四条规则。

八页共用四条规则，所以一次断言完，而不是重复写八遍：

* 每一页都要会话；拒绝的形式是重定向到登录表单，并记住浏览器本来想去哪。
* 一页只对它的侧边栏条目所登记的那些角色开放。这条最要紧 —— 导航条目与路由门
  是同一条规则的两种表达，而它的失败形态就是一个点进去是 403 的菜单项。
* 恰好一个条目被标上 `aria-current`，否则侧边栏就在谎报读者所在的位置。
* 外壳把可拖动的边界做成真控件：`role="separator"` 加 `tabindex`，于是它能被键盘
  够到，而不只是鼠标。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from api_support import Actor, build_client, create_actor, csrf_value, web_login
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import TOPIC_PUBLISH_MODES, Topic

#: 侧边栏外壳里的每一页：`(导航键, 路径, 该条目登记的角色)`。
#: 这里的角色就是契约本身，其余断言从两侧去核对它。
PAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("home", "/ui/home", ("ADMIN", "EDITOR", "VIEWER")),
    ("reports", "/ui/reports", ("ADMIN", "EDITOR", "VIEWER")),
    ("pushes", "/ui/pushes", ("ADMIN", "EDITOR")),
    ("topics", "/ui/topics", ("ADMIN", "EDITOR", "VIEWER")),
    ("repositories", "/ui/repositories", ("ADMIN", "EDITOR", "VIEWER")),
    ("runs", "/ui/runs", ("ADMIN", "EDITOR")),
    ("evaluations", "/ui/evaluations", ("ADMIN", "EDITOR")),
    ("dashboard", "/ui/dashboard", ("ADMIN", "EDITOR")),
    ("audit", "/ui/audit", ("ADMIN",)),
)

ROLES = ("ADMIN", "EDITOR", "VIEWER")

#: 任何已登录身份都打得开的一页，用例需要「随便哪一页」时就取它。
OPEN_TO_ALL = "/ui/reports"

TOPICS = "/ui/topics"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """提供一个绑定到共享测试数据库的客户端。"""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def actors(session: Session) -> dict[str, Actor]:
    """每个标准角色一个账号，都落在团队数据范围上。"""
    return {
        role: create_actor(session, username=f"pages-{role.lower()}", role=role, data_scope="team")
        for role in ROLES
    }


def _nav_links(html: str) -> list[str]:
    """返回渲染结果里每一个侧边栏条目的 href。"""
    return re.findall(r'<a class="nav-item" href="([^"]+)"', html)


def _topic_form(**overrides: str) -> dict[str, str]:
    """一份完整的创建/更新载荷，其中列表字段以逗号分隔。"""
    payload = {
        "name": "topic-from-ui",
        "description": "由界面创建",
        "publish_mode": "MANUAL",
        "languages": "python, go",
        "keywords": "llm, agent",
        "min_stars": "5",
        "window_days": "14",
        "max_candidates": "25",
        "enabled": "true",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# 会话与角色矩阵
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
def test_an_anonymous_browser_is_sent_to_login(
    client: TestClient,
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """没有会话就一页也读不了，而登录表单会记住原来的目的地。"""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/login?next={path.replace('/', '%2F')}"


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
@pytest.mark.parametrize("role", ROLES)
def test_a_page_opens_for_exactly_the_roles_that_may_see_it(
    client: TestClient,
    actors: dict[str, Actor],
    role: str,
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """路由门与侧边栏条目编码的是同一个决定。

    这里是 `403`、那里却列着条目，就是同一条规则的两半在互相打架，所以两个方向
    都要断言，而不是只验「允许的角色拿到 200」。
    """
    web_login(client, actors[role])
    response = client.get(path, follow_redirects=False)
    if role in allowed:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text
        assert "无权访问" in response.text


@pytest.mark.parametrize("role", ROLES)
def test_the_sidebar_lists_exactly_what_that_role_can_open(
    client: TestClient,
    actors: dict[str, Actor],
    role: str,
) -> None:
    """把渲染出来的菜单读回来，与这个角色实际拿得到的页面对照。

    正是这条断言把两者绑在一起：给一个会被拒的角色列了条目，它会失败；一页打得开
    却没出现在菜单里，它同样会失败。
    """
    web_login(client, actors[role])
    listing = client.get(OPEN_TO_ALL)
    assert listing.status_code == 200
    expected = {path for _, path, allowed in PAGES if role in allowed}
    assert set(_nav_links(listing.text)) == expected


@pytest.mark.parametrize(("key", "path", "allowed"), PAGES)
def test_exactly_one_sidebar_entry_is_marked_current(
    client: TestClient,
    actors: dict[str, Actor],
    key: str,
    path: str,
    allowed: tuple[str, ...],
) -> None:
    """`aria-current` 标记跟着页面走，而且永远不超过一个。"""
    web_login(client, actors["ADMIN"])
    response = client.get(path)
    assert response.status_code == 200
    assert response.text.count('aria-current="page"') == 1


# --------------------------------------------------------------------------------------
# 外壳
# --------------------------------------------------------------------------------------


def test_the_shell_carries_a_keyboard_reachable_resizer(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """侧边栏与正文之间的边界是控件，不是装饰。

    带 `tabindex` 与 aria 数值边界的 `role="separator"`，就是 WAI-ARIA 的
    窗口分隔模式。少了 `tabindex`，这条分隔线只有鼠标能用，宽度也就成了
    「会拖的那部分用户碰巧才有」的偏好。
    """
    web_login(client, actors["ADMIN"])
    html = client.get(OPEN_TO_ALL).text

    assert 'id="splitter"' in html
    assert re.search(r'<div class="splitter"[^>]*role="separator"', html)
    assert 'aria-orientation="vertical"' in html
    assert re.search(r'id="splitter"[^>]*tabindex="0"', html)
    # 记住宽度，才是「一项设置」与「一个手势」的区别。
    assert "localStorage" in html


def test_the_shell_is_absent_from_the_signed_out_pages(
    client: TestClient,
) -> None:
    """403 与 404 页面不带侧边栏：此时没有身份可以用来搭一个。"""
    page = client.get("/ui/login")
    assert page.status_code == 200
    assert 'id="splitter"' not in page.text
    assert "guest-shell" in page.text


def test_the_stylesheet_bounds_the_sidebar_and_stops_centring_the_content(
    client: TestClient,
) -> None:
    """拖拽分隔条所依赖的两条布局事实。

    边界写在 CSS 里，脚本才只需要从一个地方读它。而正文是左对齐的：把
    `max-width` 容器在外壳里居中，会在侧边栏与正文之间留下一条空带 —— 1920
    视口下约 250px 宽 —— 正是它让两者读起来像两座孤岛，而不是一台带菜单的
    控制台。
    """
    css = client.get("/ui/static/app.css").text
    assert re.search(r"--sidebar-min:\s*\d+px", css)
    assert re.search(r"--sidebar-max:\s*\d+px", css)

    rule = re.search(r"\.page \{[^}]*\}", css)
    assert rule is not None
    body = rule.group(0)
    assert "margin: 0;" in body
    assert "auto" not in body


# --------------------------------------------------------------------------------------
# 主题配置：唯一的设置面，读与写都在这里
# --------------------------------------------------------------------------------------


def test_an_admin_creates_a_topic_and_the_row_lands_in_the_database(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """走表单的写入路径，核对的是表里的行，而不是那次重定向。

    `303` 只说明处理器决定重定向；真正说明「写进去了」的是那一行。
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/ui/topics?ok=created"

    created = session.scalar(select(Topic).where(Topic.name == "topic-from-ui"))
    assert created is not None
    assert created.description == "由界面创建"
    assert created.publish_mode == "MANUAL"
    assert created.languages_json == ["python", "go"]
    assert created.keywords_json == ["llm", "agent"]
    assert created.min_stars == 5
    assert created.window_days == 14
    assert created.max_candidates == 25
    assert created.enabled is True
    assert created.created_by == actors["ADMIN"].user_id

    trail = session.scalars(
        select(AuditLog).where(AuditLog.action == "topic.create", AuditLog.result == "success")
    ).all()
    assert [row.resource_id for row in trail] == [str(created.id)]


def test_a_taken_name_is_refused_with_the_page_still_filled_in(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """重名在页面上以 `409` 回来，而不是用一个重定向把它藏起来。

    表单会带上刚填的内容重新渲染：九个字段太多，不该为一个撞了的字段重敲一遍。
    """
    workspace.add_topic(name="already-here")
    session.commit()
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(name="already-here"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "already-here" in response.text
    assert 'value="由界面创建"' in response.text

    refusals = session.scalars(
        select(AuditLog).where(AuditLog.action == "topic.create", AuditLog.result == "failed")
    ).all()
    assert len(refusals) == 1


def test_a_blank_name_is_refused_before_the_service_is_reached(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """只含空白的名字会得到带说明的 `400`，而不是一行没有名字的记录。

    数据库里没有 `NOT NULL` 能拦住它 —— 空字符串本身是个完全合格的字符串 —— 所以
    这个检查必须存在于某处，而运维者就在这里知道结果。
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(name="   "), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    # 服务层自己的消息是英文的，面向日志行。页面负责把错误码换成一句人话：
    # 中文页面上冒出 `str(exc)` 正是评审者会注意到，而只断言状态码的测试
    # 永远发现不了的东西。
    assert "主题名不能为空" in response.text
    assert session.scalar(select(Topic).where(Topic.name == "")) is None


def test_a_non_numeric_bound_is_a_message_rather_than_a_validation_dump(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """表单发来的是文本；数字字段写「八天」该变成一句话，而非一个 JSON 指针。"""
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(window_days="八天"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "观察窗口天数必须是整数" in response.text


def test_the_edit_form_writes_only_the_topic_it_belongs_to(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """更新路径；另有一行全程不动，作为对照。"""
    target = workspace.add_topic(name="editable")
    bystander = workspace.add_topic(name="untouched")
    session.commit()

    web_login(client, actors["ADMIN"])
    detail = client.get(f"{TOPICS}/{target.id}")
    assert detail.status_code == 200
    assert 'value="editable"' in detail.text

    response = client.post(
        f"{TOPICS}/{target.id}/update",
        data={
            **_topic_form(
                name="renamed-topic",
                languages="",
                keywords="rust",
                min_stars="42",
                enabled="false",
            ),
            "csrf_token": csrf_value(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/ui/topics/{target.id}?ok=updated"

    session.expire_all()
    updated = session.get(Topic, target.id)
    assert updated is not None
    # 列表字段留空表示「不加限制」，存成 NULL —— 而不是空列表，那会一个
    # 都选不中，正好与「留空」的本意相反。
    assert updated.languages_json is None
    assert updated.keywords_json == ["rust"]
    assert updated.min_stars == 42
    assert updated.enabled is False
    assert session.get(Topic, bystander.id).name == "untouched"  # type: ignore[union-attr]


def test_a_topic_with_recorded_history_cannot_be_deleted(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """删除守卫要点名那些仍然指向该主题的表。

    这些外键是 `RESTRICT` 而不是 `CASCADE`：级联会把报告一起带走，而这恰是这一页
    绝不能出现的结果。受支持的替代做法是停用主题，页面也这样写明。
    """
    topic = workspace.add_topic(name="has-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/delete",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "agent_runs" in response.text
    # 拒绝里点名的表来自 `reference_counts`，而不是从服务层那句英文解析出来的。
    assert "不能删除" in response.text
    assert "停用" in response.text
    session.expire_all()
    assert session.get(Topic, topic.id) is not None


def test_an_unreferenced_topic_is_deleted_and_the_row_is_gone(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """同一个守卫的顺利路径，只是对象换成没有东西指向它。"""
    topic = workspace.add_topic(name="disposable")
    session.commit()
    # 发送请求之前先把 id 读出来：行一旦没了，再碰这个实例只会抛
    # `ObjectDeletedError`，而不是告诉断言什么有用的东西。
    topic_id = topic.id

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic_id}/delete",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/topics?ok=deleted"
    session.expire_all()
    assert session.get(Topic, topic_id) is None


def test_toggling_flips_only_the_enabled_column(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
) -> None:
    """停用正是删除被拒时指向的那条替代路，所以它必须真的能用。"""
    topic = workspace.add_topic(name="togglable", enabled=True)
    session.commit()

    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/toggle",
        data={"enabled": "false", "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/topics?ok=disabled")
    session.expire_all()
    assert session.get(Topic, topic.id).enabled is False  # type: ignore[union-attr]


@pytest.mark.parametrize("role", ("EDITOR", "VIEWER"))
def test_only_an_admin_may_write_topics(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    actors: dict[str, Actor],
    role: str,
) -> None:
    """一个主题就定下了公开 API 的配额，以及下一次运行要花掉的模型预算。

    这与 `docs/api-contract.md` §3.1 拒绝 `POST /sources` 的理由相同，
    所以 `EDITOR` 那个只管报告的写权限，到不了这里。
    """
    topic = workspace.add_topic(name="not-yours")
    session.commit()

    web_login(client, actors[role])
    client.get(TOPICS)
    response = client.post(
        f"{TOPICS}/{topic.id}/toggle",
        data={"enabled": "false", "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
    assert response.status_code == 403
    session.expire_all()
    assert session.get(Topic, topic.id).enabled is True  # type: ignore[union-attr]


def test_a_write_without_the_csrf_field_is_refused_and_audited(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """双提交令牌在任何写入发生之前就被校验，而这次拒绝也会被归档。"""
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(TOPICS, data=_topic_form(), follow_redirects=False)
    assert response.status_code == 403
    assert session.scalar(select(Topic).where(Topic.name == "topic-from-ui")) is None

    refusals = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.csrf", AuditLog.result == "denied")
    ).all()
    assert len(refusals) == 1


def test_the_create_form_offers_exactly_the_publish_modes_the_table_accepts(
    client: TestClient,
    actors: dict[str, Actor],
) -> None:
    """每个给出的模式都来自模型里那个元组，所以表单不会提供一个会被拒的值。"""
    web_login(client, actors["ADMIN"])
    page = client.get(TOPICS)
    assert page.status_code == 200
    offered = set(re.findall(r'<option value="([A-Z_]+)"[^>]*>', page.text))
    assert set(TOPIC_PUBLISH_MODES) <= offered


def test_a_tampered_publish_mode_is_re_reported_instead_of_crashing(
    client: TestClient,
    session: Session,
    actors: dict[str, Actor],
) -> None:
    """未知的 `publish_mode` 必须重新渲染表单，而不是撞到表上的 CHECK 约束。

    API 把这个字段声明成 `Literal`，表单没有，于是被篡改的值一路落到数据库，抛出
    `IntegrityError`，把一个用户输入问题变成了 500。
    """
    web_login(client, actors["ADMIN"])
    client.get(TOPICS)
    response = client.post(
        TOPICS,
        data={**_topic_form(publish_mode="WHENEVER"), "csrf_token": csrf_value(client)},
        follow_redirects=False,
    )

    assert response.status_code == 400, response.text
    assert "发布模式" in response.text
    assert session.scalar(select(Topic).where(Topic.name == "topic-from-ui")) is None
