"""Structured console and JSON logging with secret redaction."""

import json
import logging
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .config import Settings

_REDACTION_RULES: tuple[
    tuple[re.Pattern[str], Callable[[re.Match[str]], str]],
    ...,
] = (
    (
        re.compile(
            r"(?i)[\"']?\b(api[_-]?key|password|secret|token)\b[\"']?\s*[:=]\s*[\"']?([^\"'\s,;]+)[\"']?"
        ),
        lambda match: f"{match.group(1)}=[REDACTED]",
    ),
    (
        re.compile(r"(?i)(authorization\s*:\s*bearer)\s+[^\s,;]+"),
        lambda match: f"{match.group(1)} [REDACTED]",
    ),
    (
        re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)"),
        lambda match: f"{match.group(1)}[REDACTED]{match.group(3)}",
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), lambda _: "[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), lambda _: "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), lambda _: "[REDACTED]"),
)

_MANAGED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "celery",
    "celery.worker",
    "celery.beat",
)


def redact_text(value: str) -> str:
    """Replace common credential patterns before a value is logged."""
    redacted = value
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter that redacts the final rendered message."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        request_id = getattr(record, "request_id", None)
        event = getattr(record, "event", None)
        if request_id:
            payload["request_id"] = request_id
        if event:
            payload["event"] = event
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    """Install one stdout handler with the configured format and level."""
    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = ConsoleFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)
    root.addHandler(handler)

    for logger_name in _MANAGED_LOGGERS:
        managed_logger = logging.getLogger(logger_name)
        managed_logger.handlers.clear()
        managed_logger.propagate = True
        managed_logger.setLevel(logging.NOTSET)
