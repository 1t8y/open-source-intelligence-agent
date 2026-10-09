"""Authentication endpoints and user response contracts."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.dependencies import (
    client_ip,
    get_current_user,
    get_db_session,
    request_id,
)
from open_source_intelligence.security.service import authenticate_user, get_role_codes
from open_source_intelligence.security.tokens import (
    ACCESS_TOKEN_EXPIRES_SECONDS,
    create_access_token,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Username and password submitted by a user."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    """Safe user identity returned to authenticated clients."""

    id: int
    username: str
    display_name: str
    roles: list[str]
    data_scope: str


class TokenResponse(BaseModel):
    """Access token and current user identity."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


def build_user_response(user: User) -> UserResponse:
    """Convert an ORM user into a response without sensitive fields."""
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        roles=sorted(get_role_codes(user)),
        data_scope=user.data_scope,
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> TokenResponse:
    """Authenticate a user and issue a short-lived access token."""
    user = authenticate_user(session, payload.username, payload.password)
    if user is None:
        record_audit_event(
            session,
            action="auth.login",
            resource_type="auth",
            result="failed",
            request_id=request_id(request),
            ip_address=client_ip(request),
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user, request.app.state.settings)
    record_audit_event(
        session,
        action="auth.login",
        resource_type="auth",
        result="success",
        actor_id=user.id,
        request_id=request_id(request),
        ip_address=client_ip(request),
    )
    session.commit()
    return TokenResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRES_SECONDS,
        user=build_user_response(user),
    )


@router.get("/me", response_model=UserResponse)
def current_user(user: Annotated[User, Depends(get_current_user)]) -> UserResponse:
    """Return the authenticated user's current roles and scope."""
    return build_user_response(user)
