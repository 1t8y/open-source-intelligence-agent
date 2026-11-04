"""守护应用配置的校验行为。"""

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
    """证明测试辅助不会继承开发者本机那套真实供应商配置。"""
    (tmp_path / ".env").write_text(
        "AGENT_PROVIDER=openai_compatible\nAGENT_API_BASE=https://example.com/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = build_settings()

    assert settings.agent_provider == "offline"


def test_valid_settings_load() -> None:
    """证明一份完整的本地配置能够通过校验。"""
    settings = Settings(_env_file=None, **VALID_SETTINGS)

    assert settings.app_env == "local"
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.api_port == 8000


def test_secret_value_is_masked_in_repr() -> None:
    """证明配置对象的字符串表示不会泄露应用密钥。"""
    settings = Settings(_env_file=None, **VALID_SETTINGS)

    assert "x" * 32 not in repr(settings)
    assert settings.secret_key.get_secret_value() == "x" * 32


def test_blank_optional_tokens_are_normalized_to_none() -> None:
    """证明空白的可选源令牌不会变成空的认证请求头。"""
    settings = Settings(
        _env_file=None,
        **VALID_SETTINGS,
        github_token="",
        huggingface_token="   ",
    )

    assert settings.github_token is None
    assert settings.huggingface_token is None


def test_missing_required_settings_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """证明缺了数据库、Redis 与密钥配置时会立刻失败。"""
    for name in ("DATABASE_URL", "REDIS_URL", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_rejects_example_secret() -> None:
    """证明非本地环境不能沿用文档里的示例密钥。"""
    values = {
        **VALID_SETTINGS,
        "app_env": "production",
        "secret_key": "change-me-to-a-random-secret-of-at-least-32-characters",
    }

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_unknown_explicit_setting_is_rejected() -> None:
    """证明配置里的拼写错误会报错，而不是被悄悄忽略。"""
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
    """证明非法时区与服务地址会在加载配置时就被拒绝。"""
    values = {**VALID_SETTINGS, field: value}

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
