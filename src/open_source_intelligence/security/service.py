"""Authentication queries and user credential verification."""

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.security.passwords import hash_password, verify_password


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Return a stable hash used to equalize unknown-user verification work."""
    return hash_password("invalid-user-password-placeholder")


def get_user_by_username(session: Session, username: str) -> User | None:
    """Load a user and all roles by username."""
    statement = (
        select(User)
        .where(User.username == username)
        .options(selectinload(User.role_links).selectinload(UserRole.role))
    )
    return session.scalar(statement)


def get_user_by_id(session: Session, user_id: int) -> User | None:
    """Load a user and all roles by primary key."""
    statement = (
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.role_links).selectinload(UserRole.role))
    )
    return session.scalar(statement)


def authenticate_user(session: Session, username: str, password: str) -> User | None:
    """Return an active user when credentials are valid."""
    user = get_user_by_username(session, username)
    if user is None:
        verify_password(_dummy_password_hash(), password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if user.status != "active":
        return None
    return user


def get_role_codes(user: User) -> set[str]:
    """Return the current database-backed role codes for a user."""
    return {link.role.code for link in user.role_links}


def ensure_roles(session: Session) -> dict[str, Role]:
    """Create missing standard roles and return role definitions by code."""
    definitions = {
        "ADMIN": "Administrator",
        "EDITOR": "Editor",
        "VIEWER": "Viewer",
    }
    roles: dict[str, Role] = {}
    for code, name in definitions.items():
        role = session.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, name=name)
            session.add(role)
        roles[code] = role
    session.flush()
    return roles


def create_user(
    session: Session,
    *,
    username: str,
    password_hash: str,
    display_name: str,
    role_codes: set[str],
    data_scope: str = "personal",
) -> User:
    """Create a user and assign standard roles."""
    roles = ensure_roles(session)
    unknown_roles = role_codes.difference(roles)
    if unknown_roles:
        raise ValueError(f"unknown roles: {', '.join(sorted(unknown_roles))}")
    user = User(
        username=username,
        password_hash=password_hash,
        display_name=display_name,
        data_scope=data_scope,
    )
    session.add(user)
    session.flush()
    for code in role_codes:
        session.add(UserRole(user_id=user.id, role_id=roles[code].id))
    session.flush()
    return get_user_by_id(session, user.id) or user
