"""Runtime guard for untrusted external text.

README and other external content remain useful evidence, but instruction-like lines are
never treated as trusted control input. The guard blocks those lines before the model sees
them and raises a deterministic risk signal for the verifier and run record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BLOCKED_LINE = "[BLOCKED_UNTRUSTED_INSTRUCTION]"


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """Outcome of scanning one untrusted content payload."""

    triggered: bool
    blocked_line_count: int
    reasons: tuple[str, ...] = ()


_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    ),
    ("disregard_instructions", re.compile(r"disregard\s+.*\binstructions?\b", re.I)),
    ("system_prompt", re.compile(r"\b(system|developer)\s+prompt\b", re.I)),
    ("role_override", re.compile(r"\byou\s+are\s+now\b", re.I)),
    ("tool_override", re.compile(r"\b(call|invoke|use)\s+(the\s+)?(tool|function)\b", re.I)),
    ("shell_execution", re.compile(r"\b(execute|run)\s+(a\s+)?(shell|command)\b", re.I)),
    ("database_write", re.compile(r"\b(write|insert|update|delete)\s+.*\bdatabase\b", re.I)),
    (
        "permission_escalation",
        re.compile(r"\b(grant|bypass|escalate)\s+.*\b(admin|permission|access)\b", re.I),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(reveal|print|send|leak)\s+.*\b(api[_-]?key|secret|token|password|prompt)\b", re.I
        ),
    ),
    ("zh_ignore_instructions", re.compile(r"忽略.{0,12}(之前|以上|所有).{0,8}(指令|提示)")),
    ("zh_role_override", re.compile(r"你现在是|扮演.{0,12}(系统|管理员)")),
    ("zh_tool_override", re.compile(r"(调用|执行).{0,12}(工具|函数|命令)")),
    ("zh_database_write", re.compile(r"(写入|修改|删除).{0,12}数据库")),
    ("zh_permission_escalation", re.compile(r"(提升|绕过).{0,12}(权限|管理员)")),
    ("zh_secret_exfiltration", re.compile(r"(泄露|输出|发送).{0,12}(密钥|令牌|密码|系统提示)")),
)


def inspect_untrusted_text(text: str) -> GuardDecision:
    """Return whether untrusted text contains instruction-like content."""
    reasons: set[str] = set()
    blocked_line_count = 0
    for line in text.splitlines():
        for reason, pattern in _INJECTION_PATTERNS:
            if pattern.search(line):
                reasons.add(reason)
                blocked_line_count += 1
                break
    return GuardDecision(
        triggered=blocked_line_count > 0,
        blocked_line_count=blocked_line_count,
        reasons=tuple(sorted(reasons)),
    )


def sanitize_untrusted_text(text: str) -> tuple[str, GuardDecision]:
    """Replace instruction-like lines and wrap the remaining text as untrusted data."""
    safe_lines: list[str] = []
    reasons: set[str] = set()
    blocked_line_count = 0
    for line in text.splitlines():
        blocked = False
        for reason, pattern in _INJECTION_PATTERNS:
            if pattern.search(line):
                reasons.add(reason)
                blocked_line_count += 1
                blocked = True
                break
        safe_lines.append(BLOCKED_LINE if blocked else line)

    decision = GuardDecision(
        triggered=blocked_line_count > 0,
        blocked_line_count=blocked_line_count,
        reasons=tuple(sorted(reasons)),
    )
    body = "\n".join(safe_lines).replace("</untrusted_content>", "[escaped_end_tag]")
    return f"<untrusted_content>\n{body}\n</untrusted_content>", decision


def guard_tool_payload(payload: Any) -> tuple[Any, GuardDecision]:
    """Sanitize an untrusted tool payload before it enters the model conversation."""
    if not isinstance(payload, dict):
        return payload, GuardDecision(False, 0)
    if payload.get("untrusted") is not True or not isinstance(payload.get("content"), str):
        return payload, GuardDecision(False, 0)

    safe_content, decision = sanitize_untrusted_text(payload["content"])
    guarded = {
        **payload,
        "content": safe_content,
        "guard_triggered": decision.triggered,
    }
    if decision.triggered:
        guarded["guard_reason"] = ",".join(decision.reasons)
        guarded["blocked_line_count"] = decision.blocked_line_count
    return guarded, decision
