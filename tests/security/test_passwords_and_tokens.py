"""守护密码哈希与 JWT 访问令牌。"""

from datetime import UTC, datetime

import pytest

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.security.passwords import hash_password, verify_password
from open_source_intelligence.security.tokens import create_access_token, decode_access_token


def build_settings(secret: str = "x" * 32) -> Settings:
    """为令牌用例构造一套确定的配置。"""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key=secret,
    )


def build_user() -> User:
    """为令牌用例构造一个带单一角色的游离用户。"""
    role = Role(id=1, code="ADMIN", name="Administrator")
    user = User(
        id=1,
        username="admin",
        password_hash="hash",
        display_name="Admin",
    )
    user.role_links.append(UserRole(user_id=1, role_id=1, role=role))
    return user


def test_password_hash_round_trip_and_policy() -> None:
    """证明口令不以明文保存、能校验通过，且强制了最小长度。"""
    password = "correct horse battery staple"
    password_hash = hash_password(password)

    assert password_hash != password
    assert verify_password(password_hash, password)
    assert not verify_password(password_hash, "wrong password")
    with pytest.raises(ValueError):
        hash_password("too-short")


def test_access_token_round_trip() -> None:
    """证明有效令牌携带了用户身份与当前角色。"""
    settings = build_settings()
    token = create_access_token(build_user(), settings)

    claims = decode_access_token(token, settings)

    assert claims.sub == "1"
    assert claims.username == "admin"
    assert claims.roles == ["ADMIN"]
    assert claims.exp > int(datetime.now(UTC).timestamp())


def test_access_token_rejects_wrong_secret_and_expiry() -> None:
    """证明换一把密钥或过了有效期，令牌都无法通过校验。"""
    settings = build_settings()
    token = create_access_token(build_user(), settings, expires_minutes=-1)

    with pytest.raises(ValueError):
        decode_access_token(token, settings)
    with pytest.raises(ValueError):
        decode_access_token(token, build_settings(secret="y" * 32))
