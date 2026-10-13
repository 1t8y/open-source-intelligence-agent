"""FastAPI authentication, RBAC, and audit dependencies."""

from collections.abc import Callable, Iterator
from typing import Annotated, NoReturn

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.service import get_role_codes, get_user_by_id
from open_source_intelligence.security.tokens import decode_access_token

_BEARER_SCHEME = HTTPBearer(auto_error=False)


def get_db_session(request: Request) -> Iterator[Session]:
    """Yield a database session managed by the application factory."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session


def request_id(request: Request) -> str:
    """Return the request correlation identifier assigned by middleware."""
    return str(getattr(request.state, "request_id", "unknown"))


def client_ip(request: Request) -> str | None:
    """Return the direct client address for audit logging."""
    return request.client.host if request.client is not None else None


def deny_request(
    session: Session,
    request: Request,
    *,
    action: str,
    status_code: int,
    detail: str,
    actor_id: int | None = None,
    resource_type: str = "auth",
) -> NoReturn:
    """Record a denied or failed request and raise an HTTP error."""
    record_audit_event(
        session,
        action=action,
        resource_type=resource_type,
        result="denied" if status_code == status.HTTP_403_FORBIDDEN else "failed",
        actor_id=actor_id,
        request_id=request_id(request),
        ip_address=client_ip(request),
    )
    session.commit()
    raise HTTPException(status_code=status_code, detail=detail)


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER_SCHEME)],
    session: Annotated[Session, Depends(get_db_session)],
) -> User:
    """Validate a bearer token and return the current active user."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        deny_request(
            session,
            request,
            action="auth.token",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    assert credentials is not None
    try:
        claims = decode_access_token(credentials.credentials, request.app.state.settings)
    except ValueError:
        deny_request(
            session,
            request,
            action="auth.token",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
        )
    user = get_user_by_id(session, int(claims.sub))
    if user is None or user.status != "active":
        deny_request(
            session,
            request,
            action="auth.token",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user is unavailable",
        )
    return user


def require_roles(*allowed_roles: str) -> Callable[..., User]:
    """Build a dependency that requires at least one current database role."""

    def dependency(
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_db_session)],
    ) -> User:
        if get_role_codes(user).isdisjoint(allowed_roles):
            deny_request(
                session,
                request,
                action="auth.rbac",
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
                actor_id=user.id,
            )
        return user

    return dependency
