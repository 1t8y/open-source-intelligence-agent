"""JWT access-token creation and validation."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from jwt import InvalidTokenError
from pydantic import BaseModel, ValidationError

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.identity import User

_ALGORITHM = "HS256"


class AccessTokenClaims(BaseModel):
    """Validated claims carried by an access token."""

    sub: str
    username: str
    roles: list[str]
    type: Literal["access"]
    iat: int
    exp: int


def create_access_token(
    user: User,
    settings: Settings,
    *,
    expires_minutes: int = 60,
) -> str:
    """Create a signed access token for an active user."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=expires_minutes)
    roles = sorted(link.role.code for link in user.role_links)
    payload = AccessTokenClaims(
        sub=str(user.id),
        username=user.username,
        roles=roles,
        type="access",
        iat=int(now.timestamp()),
        exp=int(expires_at.timestamp()),
    )
    return jwt.encode(
        payload.model_dump(),
        settings.secret_key.get_secret_value(),
        algorithm=_ALGORITHM,
    )


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    """Decode and validate an access token, raising on invalid input."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "username", "roles", "type", "iat", "exp"]},
        )
        claims = AccessTokenClaims.model_validate(payload)
    except (InvalidTokenError, ValidationError) as exc:
        raise ValueError("invalid access token") from exc
    if claims.type != "access":
        raise ValueError("invalid access token type")
    return claims
