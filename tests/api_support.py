"""报告 API 与后台界面测试共用的应用、操作者与报告辅助。

用例通过一个会话写入种子数据，再通过 API/界面或新开的会话做断言，
因为共享的内存数据库把同一条连接借给了每个会话：种子的写事务一直开着，
应用自己的会话就会被挡住。

这个模块放在 `tests/` 顶层（与 `support.py` 并列），
`tests/api` 与 `tests/web` 才能把它当作普通顶层模块直接导入。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import count

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import build_settings

from open_source_intelligence.api.app import create_app
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.reporting import Report, ReportSource
from open_source_intelligence.security.dependencies import get_db_session
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.web.security import CSRF_COOKIE

LOGIN_PATH = "/ui/login"

PASSWORD = "correct horse battery staple"

#: `roles.id` 只是普通主键，所以三个标准角色用固定 id。
_ROLE_IDS = {"ADMIN": 1, "EDITOR": 2, "VIEWER": 3}

#: `support.Workspace` 会把自己的用户插成 id 1，
#: 所以操作者的 id 从远高于它的位置开始。
_ACTOR_IDS = count(1001)


@dataclass(frozen=True, slots=True)
class Actor:
    """一个只持有单一角色的种子用户。"""

    user_id: int
    username: str
    role: str
    data_scope: str


def create_actor(
    session: Session,
    *,
    username: str,
    role: str,
    data_scope: str = "personal",
) -> Actor:
    """创建一个只持有单个标准角色的用户。"""
    role_row = session.scalar(select(Role).where(Role.code == role))
    if role_row is None:
        role_row = Role(id=_ROLE_IDS[role], code=role, name=role.title())
        session.add(role_row)
        session.flush()
    user = User(
        id=next(_ACTOR_IDS),
        username=username,
        password_hash=hash_password(PASSWORD),
        display_name=username,
        data_scope=data_scope,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role_row.id))
    session.commit()
    return Actor(user_id=user.id, username=username, role=role, data_scope=data_scope)


def build_client(session_factory: sessionmaker[Session]) -> TestClient:
    """构造一个依赖图指向共享测试数据库的客户端。"""
    app = create_app(build_settings())

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def auth_headers(client: TestClient, actor: Actor) -> dict[str, str]:
    """让操作者登录，并返回一个 Bearer 请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": actor.username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def configure_report(
    session: Session,
    *,
    report_id: int,
    visibility: str | None = None,
    owner_id: int | None = None,
    status: str | None = None,
    publish_mode: str | None = None,
    publish_at: datetime | None = None,
) -> Report:
    """写入本用例要变化的字段并提交，好让 API 能看到它们。"""
    report = session.get(Report, report_id)
    assert report is not None
    if visibility is not None:
        report.visibility = visibility
    report.owner_id = owner_id
    if status is not None:
        report.status = status
    if publish_mode is not None:
        report.publish_mode = publish_mode
    if publish_at is not None:
        report.publish_at = publish_at
    session.commit()
    return report


def load_report(session_factory: sessionmaker[Session], report_id: int) -> Report:
    """用一个短命会话读出一份报告。"""
    with session_factory() as session:
        report = session.get(Report, report_id)
        assert report is not None
        return report


def source_versions(session_factory: sessionmaker[Session], report_id: int) -> set[int]:
    """返回当前存在引用行的版本号集合。"""
    with session_factory() as session:
        return {
            int(row)
            for row in session.scalars(
                select(ReportSource.version).where(ReportSource.report_id == report_id)
            ).all()
        }


def audit_trail(session_factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """按插入顺序返回 `(action, result)` 二元组。"""
    with session_factory() as session:
        rows = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        return [(row.action, row.result) for row in rows]


# --------------------------------------------------------------------------------------
# 后台界面辅助
# --------------------------------------------------------------------------------------


def csrf_value(client: TestClient) -> str:
    """返回浏览器当前持有的 CSRF 值；取不到就直接失败，不做静默兜底。"""
    value = client.cookies.get(CSRF_COOKIE)
    assert value, "no CSRF cookie: render a page through the UI first"
    return value


def prime_csrf(client: TestClient, path: str = LOGIN_PATH) -> str:
    """渲染一个页面，让服务端下发 CSRF cookie，然后返回它的值。"""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200, response.text
    return csrf_value(client)


def web_login(
    client: TestClient,
    actor: Actor,
    *,
    password: str = PASSWORD,
    next_url: str = "/ui/reports",
) -> httpx.Response:
    """通过界面表单让操作者登录，并保留随之而来的会话 cookie。"""
    token = prime_csrf(client)
    return client.post(
        LOGIN_PATH,
        data={
            "csrf_token": token,
            "username": actor.username,
            "password": password,
            "next": next_url,
        },
        follow_redirects=False,
    )


def web_logout(client: TestClient) -> httpx.Response:
    """带上当前 CSRF 值提交登出表单。"""
    return client.post(
        "/ui/logout",
        data={"csrf_token": csrf_value(client)},
        follow_redirects=False,
    )
