"""Tests for rebuilding evidence from recorded tool observations.

The defect these cover was invisible to every type check in the codebase: a failed tool call and
a successful one are both JSON objects, so `isinstance(payload, dict)` accepted both. A call that
answered `TOOL_NOT_FOUND` was therefore filed as "the cached README was read", the draft cited a
document the model never received, and report generation later died on a citation whose source row
does not exist. Every test below that asserts a failure is excluded is paired with the assertion
that a success is still absorbed - otherwise "the fix" would also be satisfied by collecting
nothing at all.
"""

import json
from typing import Any

from open_source_intelligence.agent.llm.messages import (
    ChatMessage,
    ModelToolCall,
    is_tool_error_payload,
    tool_error_payload,
)
from open_source_intelligence.agent.llm.offline import _collect_evidence, compose_sections

REPO_ID = 99


def tool_call(call_id: str, name: str, repo_id: int = REPO_ID) -> ChatMessage:
    """Build the assistant turn that requested one repository tool."""
    return ChatMessage(
        role="assistant",
        tool_calls=(ModelToolCall(call_id=call_id, name=name, arguments={"repo_id": repo_id}),),
    )


def observation(call_id: str, name: str, payload: Any) -> ChatMessage:
    """Build the tool message the graph would have appended for that call."""
    return ChatMessage(
        role="tool",
        name=name,
        tool_call_id=call_id,
        content=json.dumps(payload, ensure_ascii=False),
    )


def failure(code: str = "TOOL_NOT_FOUND") -> dict[str, Any]:
    """Build the failure shell the graph renders."""
    return tool_error_payload(code, "repository content not available")


def content_payload() -> dict[str, Any]:
    """Build a successful `get_repository_content` payload."""
    return {
        "repo_id": REPO_ID,
        "source_url": "https://github.com/example/repo/blob/main/README.md",
        "content": "# Rocket",
        "content_hash": "0" * 64,
        "truncated": False,
        "untrusted": True,
    }


def profile_payload() -> dict[str, Any]:
    """Build a successful `get_repository_profile` payload."""
    return {"full_name": "example/repo", "stars": 1234}


def readme_source_refs(messages: list[ChatMessage]) -> set[str]:
    """Return the set of readme citations the draft would end up citing.

    A set because the same citation legitimately appears in more than one section ("risks" and
    "appendix" both cite the cached README when one was read); what matters here is whether the
    document is cited at all.
    """
    sections = compose_sections(_collect_evidence(messages))
    return {
        str(citation["source_ref"])
        for section in sections
        for citation in section["citations"]
        if citation["source_type"] == "readme"
    }


def test_a_failed_read_is_not_filed_as_evidence() -> None:
    """`TOOL_NOT_FOUND` says the document was not read, so it cannot become evidence."""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
        tool_call("c2", "get_repository_profile"),
        observation("c2", "get_repository_profile", failure("TOOL_ACCESS_DENIED")),
    ]

    evidence = _collect_evidence(messages)

    assert evidence.contents == {}
    assert evidence.profiles == {}
    assert evidence.repo_ids() == []


def test_a_successful_read_is_still_absorbed() -> None:
    """The control: filtering failures must not filter successes."""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", content_payload()),
        tool_call("c2", "get_repository_profile"),
        observation("c2", "get_repository_profile", profile_payload()),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.contents) == [REPO_ID]
    assert list(evidence.profiles) == [REPO_ID]


def test_a_failed_read_never_becomes_a_readme_citation() -> None:
    """The user-visible consequence: no citation to a document nobody read."""
    failed = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
    ]
    succeeded = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", content_payload()),
    ]

    assert readme_source_refs(failed) == set()
    assert readme_source_refs(succeeded) == {f"repository_contents:{REPO_ID}:readme"}


def test_a_later_success_replaces_an_earlier_failure() -> None:
    """The model may retry the same repository; only the successful read is evidence."""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
        tool_call("c2", "get_repository_content"),
        observation("c2", "get_repository_content", content_payload()),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.contents) == [REPO_ID]
    assert readme_source_refs(messages) == {f"repository_contents:{REPO_ID}:readme"}


def test_a_failed_metric_query_does_not_become_an_empty_series() -> None:
    """The list-valued branches are unaffected, and stay that way."""
    messages = [
        tool_call("c1", "query_repo_metrics"),
        observation("c1", "query_repo_metrics", failure("TOOL_NOT_FOUND")),
        tool_call("c2", "query_repo_metrics"),
        observation(
            "c2",
            "query_repo_metrics",
            [
                {"snapshot_date": "2026-10-15", "stars": 10},
                {"snapshot_date": "2026-10-16", "stars": 12},
            ],
        ),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.metrics) == [REPO_ID]
    assert len(evidence.metrics[REPO_ID]) == 2


def test_the_failure_predicate_tells_a_shell_from_a_payload() -> None:
    """The predicate is the shared judgement, so it is pinned on its own."""
    assert is_tool_error_payload(tool_error_payload("TOOL_NOT_FOUND", "gone"))
    assert is_tool_error_payload({"error": {"code": "X"}})
    assert not is_tool_error_payload(content_payload())
    assert not is_tool_error_payload([{"error": {"code": "X"}}])
    assert not is_tool_error_payload({"error": "a string is not the shell"})
    assert not is_tool_error_payload("not an object")
    assert not is_tool_error_payload(None)
