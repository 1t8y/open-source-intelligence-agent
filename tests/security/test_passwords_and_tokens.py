"""Tests for password hashing and JWT access tokens."""

from datetime import UTC, datetime

import pytest

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.security.passwords import hash_password, verify_password
from open_source_intelligence.security.tokens import create_access_token, decode_access_token


def build_settings(secret: str = "x" * 32) -> Settings:
    """Create deterministic settings for token tests."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key=secret,
    )


def build_user() -> User:
    """Create a transient user with one role for token tests."""
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
    """Passwords must use a non-plaintext hash and enforce minimum length."""
    password = "correct horse battery staple"
    password_hash = hash_password(password)

    assert password_hash != password
    assert verify_password(password_hash, password)
    assert not verify_password(password_hash, "wrong password")
    with pytest.raises(ValueError):
        hash_password("too-short")


def test_access_token_round_trip() -> None:
    """A valid token must carry the user identity and current roles."""
    settings = build_settings()
    token = create_access_token(build_user(), settings)

    claims = decode_access_token(token, settings)

    assert claims.sub == "1"
    assert claims.username == "admin"
    assert claims.roles == ["ADMIN"]
    assert claims.exp > int(datetime.now(UTC).timestamp())


def test_access_token_rejects_wrong_secret_and_expiry() -> None:
    """Tokens must fail validation across secrets or after expiry."""
    settings = build_settings()
    token = create_access_token(build_user(), settings, expires_minutes=-1)

    with pytest.raises(ValueError):
        decode_access_token(token, settings)
    with pytest.raises(ValueError):
        decode_access_token(token, build_settings(secret="y" * 32))
