"""Tests for JSON logging and credential redaction."""

import json
import logging

from open_source_intelligence.core.logging import JsonFormatter, redact_text


def test_redact_text_hides_common_credentials() -> None:
    """Credential-looking text should not survive redaction."""
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
    """Common structured and header formats must also be redacted."""
    text = 'api_key: abc123 Authorization: Bearer abcdefghijklmnop {"api_key": "xyz789"}'

    redacted = redact_text(text)

    assert "abc123" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "xyz789" not in redacted


def test_json_formatter_includes_context_and_redacts_message() -> None:
    """JSON logs should preserve context without exposing credentials."""
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
