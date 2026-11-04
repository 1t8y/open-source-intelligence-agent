"""针对不可信外部文本的运行时守卫。

README 等外部内容仍然是有用的证据，但其中的指令式语句绝不当作可信的控制输入。
守卫在模型看到这些行之前就把它们拦下，并为验证器与运行记录给出一个确定性的风险
信号。

两条规则让守卫既有效又不吵：

* **扫描面是每一个由供应商控制的字段**，而不只是 README 正文。仓库的
  `description`、它的 `topics`，以及历史报告的 `title`/`summary`，都出自写
  README 的同一批陌生人，所以它们一并被扫描 —— 见 `UNTRUSTED_TEXT_FIELDS`。
* **普通文档也会用的措辞需要一个受话人。**「Run the command below」是每份
  README 讲安装的说法；「you must run the command」则是冲着读者下的指令。
  `_ADDRESSED_PATTERNS` 里的模式只有在该行在对 Agent 说话时才会命中，因此
  一个安装小节不会被判成注入尝试。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BLOCKED_LINE = "[BLOCKED_UNTRUSTED_INSTRUCTION]"

#: 承载供应商可控自由文本的载荷字段。无论出现在哪里都会被扫描。
UNTRUSTED_TEXT_FIELDS: frozenset[str] = frozenset(
    {"content", "description", "summary", "title", "topics"}
)

#: 无条件的注入信号：在项目 README 里绝无正当用法的措辞。
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

#: 与普通文档共用措辞的信号，因此只有当该行在对 Agent 而非人类读者说话时
#: 才算数。
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
    # 特意不含「命令」二字：「请执行以下命令安装依赖」正是安装小节的惯用
    # 说法。真正的工具定向指令会点名某个工具或脚本，并对读者附加一条要求。
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
    """扫描一份不可信内容载荷的结果。"""

    triggered: bool
    blocked_line_count: int
    reasons: tuple[str, ...] = ()


def _line_reasons(line: str) -> tuple[str, ...]:
    """返回某一行触发的所有原因；该行干净时返回空元组。"""
    reasons = [reason for reason, pattern in _INJECTION_PATTERNS if pattern.search(line)]
    reasons.extend(reason for reason, pattern in _ADDRESSED_PATTERNS if pattern.search(line))
    return tuple(reasons)


def inspect_untrusted_text(text: str) -> GuardDecision:
    """判断不可信文本是否含有指令式内容。"""
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
    """替换指令式语句，并把余下文本包成不可信数据。"""
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
    """替换某个短字段里的指令式语句；干净文本原样返回。"""
    decision = inspect_untrusted_text(text)
    if not decision.triggered:
        return text
    decisions.append(decision)
    return "\n".join(BLOCKED_LINE if _line_reasons(line) else line for line in text.splitlines())


def _guard_field(value: Any, decisions: list[GuardDecision]) -> Any:
    """净化一个由供应商控制的字段，它可能是字符串或字符串列表。"""
    if isinstance(value, str):
        return _redact_lines(value, decisions)
    if isinstance(value, list):
        return [_redact_lines(item, decisions) if isinstance(item, str) else item for item in value]
    return value


def _merge(decisions: list[GuardDecision]) -> GuardDecision:
    """把逐字段的判定合并成图记录的那一个判定。"""
    triggered = [item for item in decisions if item.triggered]
    if not triggered:
        return GuardDecision(False, 0)
    return GuardDecision(
        triggered=True,
        blocked_line_count=sum(item.blocked_line_count for item in triggered),
        reasons=tuple(sorted({reason for item in triggered for reason in item.reasons})),
    )


def guard_tool_payload(payload: Any) -> tuple[Any, GuardDecision]:
    """在不可信工具载荷进入模型对话之前对它做净化。

    `UNTRUSTED_TEXT_FIELDS` 里点名的每个字段都会在任意深度被扫描，因为
    守卫的职责是覆盖整个扫描面，而不是调用方恰好记得标记的那一个字段。README
    正文会保留 `<untrusted_content>` 外壳；描述、摘要这类短字段只替换掉其中
    出格的行。
    """
    if isinstance(payload, list):
        # 返回集合的工具（历史报告摘要、主题）交回的一串记录，列表本身必须
        # 遍历，否则这些记录永远不会被扫描。
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
