"""FastAPI 的认证、RBAC 与审计依赖。"""

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
    """产出由应用工厂管理的数据库会话。"""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session


def request_id(request: Request) -> str:
    """返回中间件分配的请求关联标识。"""
    return str(getattr(request.state, "request_id", "unknown"))


def client_ip(request: Request) -> str | None:
    """返回用于审计记录的直连客户端地址。"""
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
    resource_id: str | None = None,
) -> NoReturn:
    """记录一次被拒绝或失败的请求，并抛出 HTTP 错误。

    调用方已经知道被拒绝的是哪个资源时，要把 `resource_id` 传进来。资源级的拒绝若漏掉
    它，审计行读起来就是「有人被拒绝了某件事」，不足以支撑排查；请求级的拒绝（缺少
    令牌、缺少角色）则本来就没有资源可写。
    """
    record_audit_event(
        session,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
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
    """校验 bearer 令牌并返回当前处于活跃状态的用户。"""
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
    """构造一个要求至少具备一个当前数据库角色的依赖。"""

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
