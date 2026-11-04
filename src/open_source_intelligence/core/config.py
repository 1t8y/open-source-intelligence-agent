"""从环境变量加载并校验的应用配置。

全项目只有这里定义配置项：`.env.example`、`infra/docker-compose.yml` 的兜底值、ORM 列默认值
都必须与这里的默认值保持一致，守卫在 `tests/core/test_config_alignment.py`。
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """API 与 Worker 进程共用的运行时配置，构造时即完成校验。

    `extra="forbid"` 意味着 `.env` 里出现任何非 Settings 键都会直接抛错 —— 所以 `.env`
    不能照抄 `.env.example`，后者包含部署用的辅助键。
    """

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
    #: 单次研究运行的工具调用预算。验证器要求每次运行有三类证据 —— 每个候选都要有画像和
    #: 指标、至少一份缓存 README、至少一次历史报告查询 —— 所以下限是 `候选数 × 2 + 2`。
    #: 默认候选上限 5 时下限是 12，而旧默认值 10 会被画像和指标吃光：那次运行既没有 README
    #: 也没有基线，于是每天都以 `AGENT_EVIDENCE_INCOMPLETE` 收场。
    agent_max_tool_calls: int = Field(default=20, ge=1, le=100)
    agent_max_tokens: int = Field(default=30000, ge=1, le=1_000_000)
    agent_max_cost: Decimal = Field(default=Decimal("0.50"), ge=0)
    #: 供应商计费表，单位为「美元 / 每百万 token」。DeepSeek 的 `usage` 只报 token 数、
    #: 不报金额，所以想给 `agent_runs.estimated_cost` 落一个真实金额，唯一的办法是在这里
    #: 定价。默认值是接入时 DeepSeek 非思考模式（`deepseek-chat` / `deepseek-flash` 关闭
    #: thinking）的标价；换供应商或价格变动应当改配置，而不是改代码。
    agent_input_price_per_mtok: Decimal = Field(default=Decimal("0.14"), ge=0)
    agent_output_price_per_mtok: Decimal = Field(default=Decimal("0.28"), ge=0)
    agent_max_candidates: int = Field(default=5, ge=1, le=30)
    agent_request_timeout_seconds: int = Field(default=60, ge=1, le=600)
    agent_prompts_dir: str | None = None
    #: 自动派发研究默认关闭：一次研究运行要花真金白银，让无人值守的循环每天给每个主题起
    #: 一次运行属于「花钱决策」，不该混进「默认值决策」。机制本身照常接线 —— 见
    #: `pipeline/research_dispatch.py`。
    #: 研究运行结束后是否自己走到报告。关闭时链路停在 `report_drafts` 一行，得有人手动跑
    #: `reports/cli.py --action generate` —— 这正是某个无人值守的日子只产出草稿、没有报告、
    #: 也没有邮件的原因。
    agent_auto_report_enabled: bool = True
    agent_auto_research_enabled: bool = False
    agent_daily_research_limit: int = Field(default=2, ge=1, le=50)
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
    #: 仅测试用的 Webhook 逃生口。线上 Webhook 守卫会拒绝回环地址和私网地址以阻断 SSRF，
    #: 而本地接收器恰好就跑在这些地址上；打开开关后，`local`/`test` 部署可以向 `127.0.0.1`
    #: 的接收器投递，从而用一次真实 HTTP 请求证明传输链路与 HMAC 签名都正确。两项在
    #: `staging`/`production` 一律拒绝 —— 那两个环境里守卫绝不能放宽。
    push_webhook_allow_private_targets: bool = False
    push_webhook_ca_bundle: str | None = None
    eval_enabled: bool = True
    eval_regression_tolerance: float = Field(default=0.02, ge=0.0, le=1.0)
    docs_enabled: bool = True

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """要求一个标准库能解析的 IANA 时区名。"""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """数据库连接必须使用受支持的 MySQL 驱动。"""
        if not value.startswith("mysql+pymysql://"):
            raise ValueError("database_url must use mysql+pymysql://")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        """只接受 Redis 的 TLS 或明文连接。"""
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("redis_url must use redis:// or rediss://")
        return value

    @field_validator("github_api_base", "huggingface_api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        """外部 API 的凭据与数据只允许走 HTTPS 端点。"""
        if not value.startswith("https://"):
            raise ValueError("external API base URLs must use https://")
        return value.rstrip("/")

    @field_validator("agent_prompts_dir")
    @classmethod
    def normalize_optional_dir(cls, value: str | None) -> str | None:
        """空白 Prompt 目录视为未配置，让默认值生效。"""
        if value is None or not value.strip():
            return None
        return value

    @field_validator("agent_api_base")
    @classmethod
    def validate_optional_api_base(cls, value: str | None) -> str | None:
        """已配置的模型端点必须走 HTTPS；空白值视为未配置。"""
        if value is None or not value.strip():
            return None
        if not value.startswith("https://"):
            raise ValueError("agent_api_base must use https://")
        return value.rstrip("/")

    @field_validator("push_email_from", "push_smtp_host", "push_smtp_username")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        """空白的推送配置视为未设置，渠道才能如实报告「未配置」。"""
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("push_email_from")
    @classmethod
    def validate_email_from(cls, value: str | None) -> str | None:
        """配置了发件人时，必须是一个可用的信封发件地址。"""
        if value is not None and "@" not in value:
            raise ValueError("push_email_from must be an email address")
        return value

    @field_validator("push_webhook_ca_bundle")
    @classmethod
    def normalize_ca_bundle(cls, value: str | None) -> str | None:
        """空白的 CA 证书包路径视为未设置。"""
        if value is None or not value.strip():
            return None
        return value.strip()

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
        """环境变量里的空白令牌视为未配置的凭据。"""
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_agent_provider(self) -> "Settings":
        """选用远程模型时，端点与凭据都必须给出。"""
        if self.agent_provider == "openai_compatible":
            if not self.agent_api_base:
                raise ValueError("agent_api_base is required for the openai_compatible provider")
            if self.agent_api_key is None:
                raise ValueError("agent_api_key is required for the openai_compatible provider")
        return self

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
        """除 local / test 之外，拒绝文档里那个示例占位密钥。"""
        if self.app_env not in {"local", "test"}:
            placeholder = "change-me-to-a-random-secret-of-at-least-32-characters"
            if self.secret_key.get_secret_value() == placeholder:
                raise ValueError("production secret_key must not use the example placeholder")
        return self

    @model_validator(mode="after")
    def validate_webhook_test_escapes(self) -> "Settings":
        """把 Webhook 的测试逃生口挡在 staging 与 production 之外。

        私网地址守卫就是 SSRF 防线：预发或生产部署绝不能放宽它；自定义 CA 证书包则会让投递层
        信任任意根证书。因此这两项在 `local` 与 `test` 之外一律拒绝。
        """
        if self.app_env not in {"local", "test"}:
            if self.push_webhook_allow_private_targets:
                raise ValueError(
                    "push_webhook_allow_private_targets is only allowed in local and test"
                )
            if self.push_webhook_ca_bundle is not None:
                raise ValueError("push_webhook_ca_bundle is only allowed in local and test")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内唯一的、已校验的配置实例。"""
    return Settings()


def clear_settings_cache() -> None:
    """清空配置缓存，供测试和环境变量重载使用。"""
    get_settings.cache_clear()
