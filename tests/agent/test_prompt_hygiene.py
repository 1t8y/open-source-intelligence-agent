"""Credential-hygiene tests for the research Agent.

`docs/security-and-privacy.md` §2 gives API keys and database credentials one explicit
requirement: they must not reach the database in clear text, the logs, or the prompt. The
first two have their own coverage (`tests/core/test_config.py` for the settings surface,
`tests/core/test_logging.py` for redaction). The third is asserted here, because "the model
never sees the deployment's credentials" is a property of the prompt the Agent actually
assembles rather than of any single function — and a property like that can only be checked by
running a real run and reading what came out.
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

#: The strings a leak would actually contain. Spelled in full rather than as fragments like
#: "secret": a bare word matches ordinary prose and would make the assertion vacuous.
CONFIDENTIAL_STRINGS = (
    "test-secret-key-with-at-least-32-characters",
    "app:secret@",
)

#: Shapes that the redaction layer knows how to hide, asserted here on the prompt side too.
CREDENTIAL_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
)


class PromptRecordingClient:
    """Deterministic offline client that keeps every message list it was handed."""

    def __init__(self) -> None:
        self._offline = OfflineModelClient()
        self.calls: list[tuple[ChatMessage, ...]] = []

    @property
    def provider(self) -> str:
        """Return the offline provider identifier."""
        return self._offline.provider

    @property
    def model(self) -> str:
        """Return the stub model identifier."""
        return self._offline.model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """Record the conversation, then delegate to the deterministic stub."""
        self.calls.append(tuple(messages))
        return self._offline.complete(messages, tools)


def _run(session: Session, workspace: Workspace) -> tuple[Any, PromptRecordingClient]:
    """Run one real research Agent over a two-repository topic and return its result."""
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
    """Return every string the model was shown, across every turn."""
    return [message.content for call in client.calls for message in call]


def test_agent_never_sends_its_own_credentials_to_the_model(
    session: Session,
    workspace: Workspace,
) -> None:
    """Nothing the deployment holds appears in any message handed to the model."""
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
    """The run ledger is as clean as the prompt: summaries and arguments hold no credentials.

    A credential that never reached the model could still reach the database through a tool
    summary or a recorded argument, and the ledger is the surface an operator reads.
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
    """Only the fixed tool whitelist is ever invoked, whatever the model asks for.

    `docs/security-and-privacy.md` §5 lists the five read/write tools and rules out shell,
    arbitrary HTTP, arbitrary SQL, filesystem, and publishing. The registry is the enforcement
    point, so the ledger is the place to prove nothing else ran.
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
