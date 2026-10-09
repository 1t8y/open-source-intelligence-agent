"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings for API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "open-source-intelligence-agent"
    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_timezone: str = "Asia/Shanghai"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    database_url: str
    redis_url: str
    secret_key: SecretStr = Field(min_length=32)
    github_token: SecretStr | None = None
    huggingface_token: SecretStr | None = None
    github_api_base: str = "https://api.github.com"
    huggingface_api_base: str = "https://hf-mirror.com/api"
    docs_enabled: bool = True

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Require an IANA timezone that the standard library can resolve."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Keep database connections on the supported MySQL driver."""
        if not value.startswith("mysql+pymysql://"):
            raise ValueError("database_url must use mysql+pymysql://")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        """Accept only Redis TLS or plain connections."""
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("redis_url must use redis:// or rediss://")
        return value

    @field_validator("github_api_base", "huggingface_api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        """Require HTTPS endpoints for external API credentials and data."""
        if not value.startswith("https://"):
            raise ValueError("external API base URLs must use https://")
        return value.rstrip("/")

    @field_validator("github_token", "huggingface_token", mode="after")
    @classmethod
    def normalize_optional_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Treat blank environment tokens as unconfigured credentials."""
        if value is None or not value.get_secret_value().strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one validated process-wide settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear cached settings for tests and environment reloads."""
    get_settings.cache_clear()
