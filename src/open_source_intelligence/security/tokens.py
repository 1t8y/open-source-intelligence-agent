"""JWT 访问令牌的签发与校验。"""

from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from jwt import InvalidTokenError
from pydantic import BaseModel, ValidationError

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.identity import User

_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRES_MINUTES = 60
ACCESS_TOKEN_EXPIRES_SECONDS = ACCESS_TOKEN_EXPIRES_MINUTES * 60


class AccessTokenClaims(BaseModel):
    """访问令牌携带的、已校验的声明。"""

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
    expires_minutes: int = ACCESS_TOKEN_EXPIRES_MINUTES,
) -> str:
    """为活跃用户签发一个已签名的访问令牌。"""
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
    """解码并校验访问令牌，输入非法时抛出异常。"""
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
