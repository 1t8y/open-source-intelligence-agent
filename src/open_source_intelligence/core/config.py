"""Application settings loaded from environment variables."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings for API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
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
    pipeline_schedule_enabled: bool = True
    pipeline_schedule_hour: int = Field(default=2, ge=0, le=23)
    pipeline_schedule_minute: int = Field(default=0, ge=0, le=59)
    pipeline_collect_limit: int = Field(default=20, ge=1, le=100)
    pipeline_content_limit: int = Field(default=20, ge=1, le=100)
    pipeline_task_max_retries: int = Field(default=2, ge=0, le=10)
    pipeline_retry_backoff_seconds: int = Field(default=30, ge=0, le=3600)
    agent_enabled: bool = True
    agent_provider: Literal["offline", "openai_compatible"] = "offline"
    agent_model: str = Field(default="offline-deterministic-v1", min_length=1, max_length=128)
    agent_api_base: str | None = None
    agent_api_key: SecretStr | None = None
    agent_prompt_version: str = Field(default="v1", min_length=1, max_length=64)
    agent_max_loops: int = Field(default=4, ge=1, le=20)
    agent_max_model_calls: int = Field(default=6, ge=1, le=50)
    agent_max_tool_calls: int = Field(default=10, ge=1, le=100)
    agent_max_tokens: int = Field(default=12000, ge=1, le=1_000_000)
    agent_max_cost: Decimal = Field(default=Decimal("0.50"), ge=0)
    agent_max_candidates: int = Field(default=5, ge=1, le=30)
    agent_request_timeout_seconds: int = Field(default=60, ge=1, le=600)
    agent_prompts_dir: str | None = None
    publish_delay_minutes: int = Field(default=60, ge=0, le=10080)
    publish_min_citation_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    publish_sweep_minutes: int = Field(default=15, ge=1, le=1440)
    push_enabled: bool = True
    push_auto_after_publish: bool = True
    push_delivery: Literal["offline", "live"] = "offline"
    push_max_attempts: int = Field(default=3, ge=1, le=10)
    push_retry_backoff_seconds: int = Field(default=60, ge=0, le=3600)
    push_retry_interval_minutes: int = Field(default=15, ge=1, le=1440)
    push_timeout_seconds: int = Field(default=20, ge=1, le=300)
    push_email_from: str | None = None
    push_smtp_host: str | None = None
    push_smtp_port: int = Field(default=587, ge=1, le=65535)
    push_smtp_username: str | None = None
    push_smtp_password: SecretStr | None = None
    push_smtp_starttls: bool = True
    push_webhook_signing_secret: SecretStr | None = None
    eval_enabled: bool = True
    eval_regression_tolerance: float = Field(default=0.02, ge=0.0, le=1.0)
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

    @field_validator("agent_prompts_dir")
    @classmethod
    def normalize_optional_dir(cls, value: str | None) -> str | None:
        """Treat a blank prompt directory as unset so defaults can apply."""
        if value is None or not value.strip():
            return None
        return value

    @field_validator("agent_api_base")
    @classmethod
    def validate_optional_api_base(cls, value: str | None) -> str | None:
        """Require HTTPS for a configured model endpoint and drop blank values."""
        if value is None or not value.strip():
            return None
        if not value.startswith("https://"):
            raise ValueError("agent_api_base must use https://")
        return value.rstrip("/")

    @field_validator("push_email_from", "push_smtp_host", "push_smtp_username")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        """Treat a blank push setting as unset so a channel can report it is not configured."""
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("push_email_from")
    @classmethod
    def validate_email_from(cls, value: str | None) -> str | None:
        """Require a usable envelope sender when one is configured."""
        if value is not None and "@" not in value:
            raise ValueError("push_email_from must be an email address")
        return value

    @field_validator(
        "github_token",
        "huggingface_token",
        "agent_api_key",
        "push_smtp_password",
        "push_webhook_signing_secret",
        mode="after",
    )
    @classmethod
    def normalize_optional_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Treat blank environment tokens as unconfigured credentials."""
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_agent_provider(self) -> "Settings":
        """Require endpoint and credential whenever a remote model is selected."""
        if self.agent_provider == "openai_compatible":
            if not self.agent_api_base:
                raise ValueError("agent_api_base is required for the openai_compatible provider")
            if self.agent_api_key is None:
                raise ValueError("agent_api_key is required for the openai_compatible provider")
        return self

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
        """Reject the documented placeholder outside local and test use."""
        if self.app_env not in {"local", "test"}:
            placeholder = "change-me-to-a-random-secret-of-at-least-32-characters"
            if self.secret_key.get_secret_value() == placeholder:
                raise ValueError("production secret_key must not use the example placeholder")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one validated process-wide settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear cached settings for tests and environment reloads."""
    get_settings.cache_clear()
