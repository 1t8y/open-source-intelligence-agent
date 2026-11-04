"""守护 JSON 日志与凭据脱敏。"""

import json
import logging

from open_source_intelligence.core.logging import JsonFormatter, redact_text


def test_redact_text_hides_common_credentials() -> None:
    """证明看起来像凭据的文本经脱敏后不会残留。"""
    text = (
        "api_key=abc123 token=secret-value "
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
        "mysql+pymysql://app:topsecret@db:3306/app"
    )

    redacted = redact_text(text)

    assert "abc123" not in redacted
    assert "secret-value" not in redacted
    assert "ghp_" not in redacted
    assert "topsecret" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_handles_colon_json_and_bearer_forms() -> None:
    """证明结构化写法与请求头写法同样会被脱敏。"""
    text = 'api_key: abc123 Authorization: Bearer abcdefghijklmnop {"api_key": "xyz789"}'

    redacted = redact_text(text)

    assert "abc123" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "xyz789" not in redacted


def test_json_formatter_includes_context_and_redacts_message() -> None:
    """证明 JSON 日志保留上下文字段，同时又不会带出凭据。"""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling provider api_key=abc123",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-123"
    record.event = "provider.call"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-123"
    assert payload["event"] == "provider.call"
    assert "abc123" not in payload["message"]
