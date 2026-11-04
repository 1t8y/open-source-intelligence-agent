"""守护登录、RBAC 与审计：每一次放行和拒绝都要在 `audit_logs` 里留下痕迹。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.security.dependencies import get_db_session, require_roles
from open_source_intelligence.security.passwords import hash_password


def build_auth_app(role_code: str = "ADMIN") -> tuple[TestClient, sessionmaker[Session], str]:
    """构造一个由共享的内存 SQLite 数据库支撑的测试应用。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
    )
    app = create_app(settings)
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    password = "correct horse battery staple"
    with session_factory() as session:
        role = Role(id=1, code=role_code, name=role_code.title())
        user = User(
            id=1,
            username="test-user",
            password_hash=hash_password(password),
            display_name="Test User",
        )
        session.add_all([role, user])
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session

    @app.get("/test/admin")
    def admin_only(user: Annotated[User, Depends(require_roles("ADMIN"))]) -> dict[str, str]:
        return {"username": user.username}

    return TestClient(app), session_factory, password


def test_login_success_returns_token_and_audit_event() -> None:
    """证明正确的凭据既换回可用的令牌，也留下一条成功审计。"""
    client, session_factory, password = build_auth_app()
    with client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "test-user", "password": password},
        )
        token = response.json()["access_token"]
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["user"]["roles"] == ["ADMIN"]
    assert me_response.status_code == 200
    assert me_response.json()["username"] == "test-user"
    with session_factory() as session:
        events = session.query(AuditLog).all()
    assert [(event.action, event.result) for event in events] == [("auth.login", "success")]


def test_login_failure_is_audited() -> None:
    """证明错误的凭据返回 401，并且审计表里留下一条失败记录。"""
    client, session_factory, _ = build_auth_app()
    with client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "test-user", "password": "wrong password"},
        )

    assert response.status_code == 401
    with session_factory() as session:
        event = session.query(AuditLog).one()
    assert event.action == "auth.login"
    assert event.result == "failed"


def test_missing_token_is_audited() -> None:
    """证明受保护端点会拒绝并审计缺失的凭据。"""
    client, session_factory, _ = build_auth_app()
    with client:
        response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    with session_factory() as session:
        event = session.query(AuditLog).one()
    assert event.action == "auth.token"
    assert event.result == "failed"


def test_rbac_denial_is_audited() -> None:
    """证明数据库里的当前角色不在白名单内时必须收到 403。"""
    client, session_factory, password = build_auth_app(role_code="VIEWER")
    with client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "test-user", "password": password},
        )
        token = login.json()["access_token"]
        response = client.get(
            "/test/admin",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    with session_factory() as session:
        events = session.query(AuditLog).order_by(AuditLog.id).all()
    assert [(event.action, event.result) for event in events] == [
        ("auth.login", "success"),
        ("auth.rbac", "denied"),
    ]
