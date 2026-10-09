"""Tests for validated application settings."""

import pytest
from pydantic import ValidationError

from open_source_intelligence.core.config import Settings

VALID_SETTINGS = {
    "database_url": "mysql+pymysql://app:test@localhost:3306/intelligence",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "x" * 32,
}


def test_valid_settings_load() -> None:
    """A complete local configuration should validate."""
    settings = Settings(_env_file=None, **VALID_SETTINGS)

    assert settings.app_env == "local"
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.api_port == 8000


def test_secret_value_is_masked_in_repr() -> None:
    """Settings representations must not expose the application secret."""
    settings = Settings(_env_file=None, **VALID_SETTINGS)

    assert "x" * 32 not in repr(settings)
    assert settings.secret_key.get_secret_value() == "x" * 32


def test_missing_required_settings_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing database, Redis, and secret configuration must fail fast."""
    for name in ("DATABASE_URL", "REDIS_URL", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("app_timezone", "Mars/Olympus"),
        ("database_url", "sqlite:///test.db"),
        ("redis_url", "http://localhost:6379"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(field: str, value: str) -> None:
    """Invalid timezone and service URLs must fail during settings loading."""
    values = {**VALID_SETTINGS, field: value}

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
