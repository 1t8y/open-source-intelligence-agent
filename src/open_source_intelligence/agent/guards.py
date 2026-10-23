"""Runtime guard for untrusted external text.

README and other external content remain useful evidence, but instruction-like lines are
never treated as trusted control input. The guard blocks those lines before the model sees
them and raises a deterministic risk signal for the verifier and run record.

Two rules keep the guard both effective and quiet:

* **The surface is every provider-controlled field**, not just a README body. A repository's
  `description`, its `topics`, and a prior report's `title`/`summary` are written by the same
  strangers who write the README, so they are scanned too — `UNTRUSTED_TEXT_FIELDS`.
* **Wording that normal documentation also uses needs an addressee.** "Run the command below" is
  how every README explains installation; "you must run the command" is an instruction aimed at
  the reader. Patterns in `_ADDRESSED_PATTERNS` only fire when the line addresses an agent, so
  an install section is not scored as an injection attempt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BLOCKED_LINE = "[BLOCKED_UNTRUSTED_INSTRUCTION]"

#: Payload fields that carry provider-controlled free text. Scanned wherever they appear.
UNTRUSTED_TEXT_FIELDS: frozenset[str] = frozenset(
    {"content", "description", "summary", "title", "topics"}
)

#: Unconditional injection signals: phrasing that has no innocent use in a project README.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"ignore\s+(all\s+)?(of\s+)?(the\s+)?(previous|prior|above)\s+instructions",
            re.I,
        ),
    ),
    ("disregard_instructions", re.compile(r"disregard\s+.*\binstructions?\b", re.I)),
    ("system_prompt", re.compile(r"\b(system|developer)\s+prompt\b", re.I)),
    ("role_override", re.compile(r"\byou\s+are\s+now\b", re.I)),
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
    ("zh_permission_escalation", re.compile(r"(提升|绕过).{0,12}(权限|管理员)")),
    ("zh_secret_exfiltration", re.compile(r"(泄露|输出|发送).{0,12}(密钥|令牌|密码|系统提示)")),
)

#: Signals that share their wording with ordinary documentation, so they only count when the
#: line addresses an agent rather than a human reader.
_ADDRESSED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tool_override",
        re.compile(
            r"\b(?:you\s+(?:must|should|need\s+to)|please)\s+(?:call|invoke|use)\s+"
            r"(?:the\s+)?(?:tool|function)\b",
            re.I,
        ),
    ),
    (
        "shell_execution",
        re.compile(
            r"\b(?:you\s+(?:must|should|need\s+to)|please)\s+(?:execute|run)\s+"
            r"(?:a\s+)?(?:shell|command)\b",
            re.I,
        ),
    ),
    (
        "database_write",
        re.compile(
            r"\b(?:you\s+(?:must|should|need\s+to)|please)\s+(?:write|insert|update|delete)\s+"
            r".*\bdatabase\b",
            re.I,
        ),
    ),
    # Deliberately no `命令`: "请执行以下命令安装依赖" is what an install section says. A real
    # tool-directed instruction names a tool or a script, and adds a directive to the reader.
    (
        "zh_tool_override",
        re.compile(r"(请|你|您|必须|立即|立刻|现在).{0,6}(调用|执行|运行).{0,6}(工具|函数|脚本)"),
    ),
    (
        "zh_database_write",
        re.compile(r"(请|你|您|必须|立即|立刻).{0,6}(写入|修改|删除).{0,8}数据库"),
    ),
)


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """Outcome of scanning one untrusted content payload."""

    triggered: bool
    blocked_line_count: int
    reasons: tuple[str, ...] = ()


def _line_reasons(line: str) -> tuple[str, ...]:
    """Return every reason one line trips, or an empty tuple when it is clean."""
    reasons = [reason for reason, pattern in _INJECTION_PATTERNS if pattern.search(line)]
    reasons.extend(reason for reason, pattern in _ADDRESSED_PATTERNS if pattern.search(line))
    return tuple(reasons)


def inspect_untrusted_text(text: str) -> GuardDecision:
    """Return whether untrusted text contains instruction-like content."""
    reasons: set[str] = set()
    blocked_line_count = 0
    for line in text.splitlines():
        hit = _line_reasons(line)
        if hit:
            reasons.update(hit)
            blocked_line_count += 1
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
        hit = _line_reasons(line)
        if hit:
            reasons.update(hit)
            blocked_line_count += 1
            safe_lines.append(BLOCKED_LINE)
            continue
        safe_lines.append(line)

    decision = GuardDecision(
        triggered=blocked_line_count > 0,
        blocked_line_count=blocked_line_count,
        reasons=tuple(sorted(reasons)),
    )
    body = "\n".join(safe_lines).replace("</untrusted_content>", "[escaped_end_tag]")
    return f"<untrusted_content>\n{body}\n</untrusted_content>", decision


def _redact_lines(text: str, decisions: list[GuardDecision]) -> str:
    """Replace instruction-like lines in one short field, leaving clean text untouched."""
    decision = inspect_untrusted_text(text)
    if not decision.triggered:
        return text
    decisions.append(decision)
    return "\n".join(BLOCKED_LINE if _line_reasons(line) else line for line in text.splitlines())


def _guard_field(value: Any, decisions: list[GuardDecision]) -> Any:
    """Redact one provider-controlled field, which may be a string or a list of strings."""
    if isinstance(value, str):
        return _redact_lines(value, decisions)
    if isinstance(value, list):
        return [_redact_lines(item, decisions) if isinstance(item, str) else item for item in value]
    return value


def _merge(decisions: list[GuardDecision]) -> GuardDecision:
    """Fold per-field decisions into the single decision the graph records."""
    triggered = [item for item in decisions if item.triggered]
    if not triggered:
        return GuardDecision(False, 0)
    return GuardDecision(
        triggered=True,
        blocked_line_count=sum(item.blocked_line_count for item in triggered),
        reasons=tuple(sorted({reason for item in triggered for reason in item.reasons})),
    )


def guard_tool_payload(payload: Any) -> tuple[Any, GuardDecision]:
    """Sanitize an untrusted tool payload before it enters the model conversation.

    Every field named in `UNTRUSTED_TEXT_FIELDS` is scanned, at any depth, because the guard's
    job is to cover the surface rather than the one field a caller remembered to mark. A README
    body keeps its `<untrusted_content>` wrapper; a short field such as a description or a
    summary only has its offending lines replaced.
    """
    if isinstance(payload, list):
        # Tools that return a collection (prior report summaries, topics) hand back a list of
        # records, so the list itself has to be walked or those records are never scanned.
        item_decisions: list[GuardDecision] = []
        items: list[Any] = []
        list_changed = False
        for item in payload:
            guarded_item, item_decision = guard_tool_payload(item)
            item_decisions.append(item_decision)
            list_changed = list_changed or guarded_item is not item
            items.append(guarded_item)
        decision = _merge(item_decisions)
        return (items if list_changed else payload), decision

    if not isinstance(payload, dict):
        return payload, GuardDecision(False, 0)

    is_untrusted = payload.get("untrusted") is True
    decisions: list[GuardDecision] = []
    guarded: dict[str, Any] = {}
    changed = False
    for key, value in payload.items():
        if key == "content" and is_untrusted and isinstance(value, str):
            safe_content, decision = sanitize_untrusted_text(value)
            decisions.append(decision)
            guarded[key] = safe_content
            changed = True
            continue
        if key in UNTRUSTED_TEXT_FIELDS:
            guarded_value = _guard_field(value, decisions)
            changed = changed or guarded_value != value
            guarded[key] = guarded_value
            continue
        if isinstance(value, (Mapping, list)):
            nested, nested_decision = guard_tool_payload(value)
            decisions.append(nested_decision)
            changed = changed or nested is not value
            guarded[key] = nested
            continue
        guarded[key] = value

    decision = _merge(decisions)
    if not changed:
        return payload, decision
    result = {**guarded, "guard_triggered": decision.triggered}
    if decision.triggered:
        result["guard_reason"] = ",".join(decision.reasons)
        result["blocked_line_count"] = decision.blocked_line_count
    return result, decision
