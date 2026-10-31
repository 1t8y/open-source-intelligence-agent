"""Tests for validated application settings."""

import pytest
from pydantic import ValidationError
from support import build_settings

from open_source_intelligence.core.config import Settings

VALID_SETTINGS = {
    "database_url": "mysql+pymysql://app:test@localhost:3306/intelligence",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "x" * 32,
}


def test_test_settings_ignore_a_developer_dotenv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test helpers must not inherit a developer's real provider configuration."""
    (tmp_path / ".env").write_text(
        "AGENT_PROVIDER=openai_compatible\nAGENT_API_BASE=https://example.com/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = build_settings()

    assert settings.agent_provider == "offline"


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


def test_blank_optional_tokens_are_normalized_to_none() -> None:
    """Blank optional source tokens must not create empty auth headers."""
    settings = Settings(
        _env_file=None,
        **VALID_SETTINGS,
        github_token="",
        huggingface_token="   ",
    )

    assert settings.github_token is None
    assert settings.huggingface_token is None


def test_missing_required_settings_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing database, Redis, and secret configuration must fail fast."""
    for name in ("DATABASE_URL", "REDIS_URL", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_rejects_example_secret() -> None:
    """Non-local environments must not use the documented example secret."""
    values = {
        **VALID_SETTINGS,
        "app_env": "production",
        "secret_key": "change-me-to-a-random-secret-of-at-least-32-characters",
    }

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_unknown_explicit_setting_is_rejected() -> None:
    """Configuration typos must fail instead of being silently ignored."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **VALID_SETTINGS, unknown_setting=True)


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
