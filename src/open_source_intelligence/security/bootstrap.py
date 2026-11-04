"""初始化标准角色与初始管理员账号。"""

import argparse
import os

from sqlalchemy.orm import Session

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.models.identity import User, UserRole
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import (
    create_user,
    ensure_roles,
    get_user_by_username,
)


def bootstrap_admin(
    session: Session,
    *,
    username: str,
    password: str,
    display_name: str,
) -> User:
    """创建或提升一个管理员，并落一条审计事件。"""
    roles = ensure_roles(session)
    user = get_user_by_username(session, username)
    if user is None:
        user = create_user(
            session,
            username=username,
            password_hash=hash_password(password),
            display_name=display_name,
            role_codes={"ADMIN"},
            data_scope="team",
        )
    else:
        user.password_hash = hash_password(password)
        user.display_name = display_name
        user.status = "active"
        user.data_scope = "team"
        if "ADMIN" not in {link.role.code for link in user.role_links}:
            session.add(UserRole(user_id=user.id, role_id=roles["ADMIN"].id))
        session.flush()
        user = get_user_by_username(session, username) or user

    record_audit_event(
        session,
        action="auth.bootstrap_admin",
        resource_type="user",
        resource_id=str(user.id),
        result="success",
        actor_id=user.id,
        metadata_json={"username": username},
    )
    session.commit()
    return user


def parse_args() -> argparse.Namespace:
    """解析初始化 CLI 参数，且不从命令行参数接收密码。"""
    parser = argparse.ArgumentParser(description="Create standard roles and an admin user")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--display-name", default="Administrator")
    return parser.parse_args()


def main() -> None:
    """针对已配置的数据库执行初始化。"""
    args = parse_args()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("BOOTSTRAP_ADMIN_PASSWORD is required")

    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        user = bootstrap_admin(
            session,
            username=args.username,
            password=password,
            display_name=args.display_name,
        )
        print(f"bootstrap_admin_ready={user.username}")
    engine.dispose()


if __name__ == "__main__":
    main()
