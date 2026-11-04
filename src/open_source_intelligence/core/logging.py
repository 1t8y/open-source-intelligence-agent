"""结构化日志：控制台与 JSON 两种格式，出站前统一脱敏。

脱敏发生在**最后一步**（渲染后的字符串），而不是在每个调用点：只要有一条日志路径忘了
`redact_text`，密钥就会落到磁盘。规则表 `_REDACTION_RULES` 覆盖 `key=value` 形态的
凭据、Bearer 头、URL 内的用户名密码，以及 `sk-` / `ghp_` / `github_pat_` 三种已知前缀。
"""

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
    """在写入日志前，把常见的凭据形态替换成 `[REDACTED]`。"""
    redacted = value
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class ConsoleFormatter(logging.Formatter):
    """人类可读的控制台格式器：对渲染完成的整条消息再做一次脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonFormatter(logging.Formatter):
    """每条日志记录输出一个 JSON 对象，附带可选的 `request_id` 与 `event`。"""

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
    """把唯一的 stdout handler 装到根 logger 上，格式与级别取自配置。

    uvicorn / celery 自带的 handler 会被清空并改为向根 logger 传播，否则同一个进程里会出现
    两套格式（容器里就是两行一条日志，一行 JSON 一行纯文本）。
    """
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
