"""研究 Agent 的凭据卫生用例。

`docs/security-and-privacy.md` §2 对 API 密钥与数据库凭据提了一条明确要求：它们不得以
明文进入数据库、日志或 Prompt。前两者各有覆盖（`tests/core/test_config.py` 管配置面，
`tests/core/test_logging.py` 管脱敏）。第三条在这里断言，因为「模型永远看不到部署的
凭据」是 Agent 实际组装出的 Prompt 的属性，而不是某个函数的属性 —— 这类属性只能靠跑
一次真实运行、再读它吐出来的东西来验证。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from agent_support import seed_topic
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.llm.messages import ChatMessage, ModelTurn, ToolSchema
from open_source_intelligence.agent.llm.offline import OfflineModelClient
from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.persistence.models.agent import AgentStep, ToolCall

#: 泄漏时真正会出现的字符串。这里写全整串，而不是用 "secret" 这类片段：
#: 一个光秃秃的词会匹配到普通叙述，让断言变成空转。
CONFIDENTIAL_STRINGS = (
    "test-secret-key-with-at-least-32-characters",
    "app:secret@",
)

#: 脱敏层认得、也知道怎么遮的形态；这里在 Prompt 侧同样断言一遍。
CREDENTIAL_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
)


class PromptRecordingClient:
    """确定性的离线客户端，保留它收到的每一份消息列表。"""

    def __init__(self) -> None:
        self._offline = OfflineModelClient()
        self.calls: list[tuple[ChatMessage, ...]] = []

    @property
    def provider(self) -> str:
        """返回离线供应商标识。"""
        return self._offline.provider

    @property
    def model(self) -> str:
        """返回桩模型标识。"""
        return self._offline.model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """先记下这次对话，再交给确定性桩处理。"""
        self.calls.append(tuple(messages))
        return self._offline.complete(messages, tools)


def _run(session: Session, workspace: Workspace) -> tuple[Any, PromptRecordingClient]:
    """在一个含两个仓库的主题上跑一次真实研究 Agent，返回其结果。"""
    seeded = seed_topic(workspace, repo_count=2)
    client = PromptRecordingClient()
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        client=client,
        now=NOW,
    )
    return result, client


def _shown(client: PromptRecordingClient) -> list[str]:
    """返回模型在所有轮次里看到过的每一个字符串。"""
    return [message.content for call in client.calls for message in call]


def test_agent_never_sends_its_own_credentials_to_the_model(
    session: Session,
    workspace: Workspace,
) -> None:
    """证明部署持有的一切都不会出现在递给模型的任何消息里。"""
    result, client = _run(session, workspace)

    assert result.status == "completed"
    shown = _shown(client)
    assert shown, "the Agent assembled no prompt at all"

    for confidential in CONFIDENTIAL_STRINGS:
        assert all(confidential not in text for text in shown), confidential
    for shape in CREDENTIAL_SHAPES:
        assert not any(shape.search(text) for text in shown), shape.pattern


def test_persisted_agent_evidence_carries_no_credentials(
    session: Session,
    workspace: Workspace,
) -> None:
    """证明运行台账和 Prompt 一样干净：摘要与参数里都没有凭据。

    一份从未抵达模型的凭据，仍可能经工具摘要或记录下来的参数进入数据库，而台账正是
    运维要读的那一面。
    """
    result, _ = _run(session, workspace)

    steps = session.scalars(
        select(AgentStep).where(AgentStep.agent_run_id == result.agent_run_id)
    ).all()
    calls = session.scalars(
        select(ToolCall).where(ToolCall.agent_run_id == result.agent_run_id)
    ).all()
    assert steps, "the run recorded no steps"
    assert calls, "the run recorded no tool calls"

    persisted = [step.summary or "" for step in steps]
    persisted += [call.result_summary or "" for call in calls]
    persisted += [str(call.arguments_json) for call in calls]
    for confidential in CONFIDENTIAL_STRINGS:
        assert all(confidential not in text for text in persisted), confidential


def test_run_ledger_carries_only_whitelisted_tools(
    session: Session,
    workspace: Workspace,
) -> None:
    """证明无论模型要什么，被调用的都只有那份固定工具白名单。

    `docs/security-and-privacy.md` §5 列出了五个读/写工具，并排除了 shell、任意
    HTTP、任意 SQL、文件系统与发布。注册表是执行点，因此台账是证明「没有别的东西
    跑过」的地方。
    """
    result, _ = _run(session, workspace)
    calls = session.scalars(
        select(ToolCall).where(ToolCall.agent_run_id == result.agent_run_id)
    ).all()
    assert {call.tool_name for call in calls} <= {
        "query_repo_metrics",
        "get_repository_profile",
        "get_repository_content",
        "search_prior_reports",
        "save_report_section",
    }
    assert all(call.status in {"success", "failed", "denied"} for call in calls)
