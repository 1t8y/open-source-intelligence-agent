"""Tests for the model provider adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import httpx
import pytest
from support import build_settings

from open_source_intelligence.agent.llm.errors import (
    MODEL_INVALID_RESPONSE,
    MODEL_RATE_LIMITED,
    MODEL_REQUEST_FAILED,
    MODEL_TIMEOUT,
    ModelClientError,
)
from open_source_intelligence.agent.llm.factory import build_model_client
from open_source_intelligence.agent.llm.messages import (
    CANDIDATE_TAG,
    TOPIC_TAG,
    WINDOW_TAG,
    ChatMessage,
    ModelToolCall,
    ToolSchema,
)
from open_source_intelligence.agent.llm.offline import OfflineModelClient
from open_source_intelligence.agent.llm.openai_compatible import OpenAICompatibleClient
from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS

Handler = Callable[[httpx.Request], httpx.Response]

WINDOW = '{"start": "2026-10-05T12:00:00+00:00", "end": "2026-10-12T12:00:00+00:00"}'

TOOLS = (ToolSchema(name="get_repository_profile", description="profile", parameters={}),)


def _plan_messages(candidates: Sequence[int] = (1, 2)) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="plan"),
        ChatMessage(
            role="user",
            content=(
                f"<{TOPIC_TAG}>7</{TOPIC_TAG}>"
                f"<{CANDIDATE_TAG}>{json.dumps(list(candidates))}</{CANDIDATE_TAG}>"
                f"<{WINDOW_TAG}>{WINDOW}</{WINDOW_TAG}>"
            ),
        ),
    ]


def _client(handler: Handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        api_base="https://api.example.com/v1",
        api_key="test-key",
        model="gpt-4o-mini",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _priced_client(handler: Handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        api_base="https://api.example.com/v1",
        api_key="test-key",
        model="gpt-4o-mini",
        input_price_per_mtok=Decimal("0.14"),
        output_price_per_mtok=Decimal("0.28"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_offline_provider_identity() -> None:
    client = OfflineModelClient()
    assert client.provider == "offline"
    assert client.model == "offline-deterministic-v1"


def test_offline_plan_is_deterministic() -> None:
    client = OfflineModelClient()
    messages = _plan_messages()
    first = client.complete(messages, TOOLS)
    second = client.complete(messages, TOOLS)
    assert first == second
    assert [call.name for call in first.tool_calls] == ["get_repository_profile"] * 2
    assert [call.arguments["repo_id"] for call in first.tool_calls] == [1, 2]


def test_offline_reads_run_context_from_user_messages_only() -> None:
    """Placeholder blocks in the system prompt must never be mistaken for run context."""
    system = (
        '<research_window>{"start": "...", "end": "..."}</research_window>'
        "<candidate_repos>[9]</candidate_repos>"
    )
    messages = [ChatMessage(role="system", content=system), *_plan_messages((4,))[1:]]
    turn = OfflineModelClient().complete(messages, TOOLS)
    assert [call.arguments["repo_id"] for call in turn.tool_calls] == [4]


def test_offline_stops_without_candidates() -> None:
    turn = OfflineModelClient().complete(_plan_messages(()), TOOLS)
    assert turn.tool_calls == ()
    assert "候选" in turn.text


def test_offline_advances_only_after_observed_results() -> None:
    client = OfflineModelClient()
    messages = _plan_messages((1,))
    first = client.complete(messages, TOOLS)
    observed = [
        *messages,
        ChatMessage(role="assistant", content="", tool_calls=first.tool_calls),
        ChatMessage(role="tool", name="get_repository_profile", tool_call_id="x", content="{}"),
    ]
    second = client.complete(observed, TOOLS)
    assert [call.name for call in second.tool_calls] == ["query_repo_metrics"]


def test_offline_write_turn_covers_every_section() -> None:
    turn = OfflineModelClient().complete([], ())
    payload: Any = json.loads(turn.text)
    keys = [section["section_key"] for section in payload["sections"]]
    assert keys == list(REPORT_SECTION_KEYS)
    assert turn.tool_calls == ()


def _observed_trail() -> list[ChatMessage]:
    """Rebuild the recorded tool trail of a first research run with no prior reports."""
    calls = (
        ModelToolCall(call_id="profile-1", name="get_repository_profile", arguments={"repo_id": 1}),
        ModelToolCall(call_id="metrics-1", name="query_repo_metrics", arguments={"repo_id": 1}),
        ModelToolCall(call_id="content-1", name="get_repository_content", arguments={"repo_id": 1}),
        ModelToolCall(call_id="prior-1", name="search_prior_reports", arguments={"topic_id": 7}),
    )
    observations: list[tuple[str, str, Any]] = [
        ("get_repository_profile", "profile-1", {"full_name": "acme/repo-1"}),
        (
            "query_repo_metrics",
            "metrics-1",
            [
                {"stars": 100, "forks": 10, "snapshot_date": "2026-10-05"},
                {"stars": 140, "forks": 12, "snapshot_date": "2026-10-11"},
            ],
        ),
        ("get_repository_content", "content-1", {"content": "# README"}),
        ("search_prior_reports", "prior-1", []),
    ]
    return [
        ChatMessage(role="system", content="draft"),
        ChatMessage(role="assistant", content="", tool_calls=calls),
        *(
            ChatMessage(role="tool", name=name, tool_call_id=call_id, content=json.dumps(payload))
            for name, call_id, payload in observations
        ),
    ]


def test_offline_draft_never_saves_a_section_without_citations() -> None:
    """Without prior reports every section must still cite the observations behind it."""
    turn = OfflineModelClient().complete(_observed_trail(), ())
    sections: Any = json.loads(turn.text)["sections"]
    by_key = {section["section_key"]: section for section in sections}
    assert list(by_key) == list(REPORT_SECTION_KEYS)
    for key, section in by_key.items():
        assert section["citations"], f"section {key} carries no citation"
    assert {item["source_type"] for item in by_key["recommendation"]["citations"]} == {"metric"}
    assert {item["source_type"] for item in by_key["appendix"]["citations"]} == {
        "metric",
        "readme",
    }
    assert by_key["recommendation"]["citations"] == by_key["summary"]["citations"]


def test_factory_defaults_to_the_offline_provider() -> None:
    client = build_model_client(build_settings())
    assert isinstance(client, OfflineModelClient)


def test_factory_builds_the_openai_compatible_client() -> None:
    settings = build_settings(
        agent_provider="openai_compatible",
        agent_api_base="https://api.example.com/v1",
        agent_api_key="secret-key",
        agent_model="gpt-4o-mini",
    )
    client = build_model_client(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "openai_compatible"
    assert client.model == "gpt-4o-mini"


def test_openai_provider_requires_credentials() -> None:
    with pytest.raises(ValueError):
        build_settings(agent_provider="openai_compatible")


def test_openai_provider_rejects_a_plain_http_endpoint() -> None:
    with pytest.raises(ValueError):
        build_settings(
            agent_provider="openai_compatible",
            agent_api_base="http://insecure.example.com",
            agent_api_key="secret-key",
        )


def test_openai_client_parses_text_tool_calls_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "thinking",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_repository_profile",
                                        "arguments": '{"repo_id": 3}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )

    turn = _client(handler).complete([ChatMessage(role="user", content="hi")], ())
    assert turn.text == "thinking"
    assert turn.model == "gpt-4o-mini"
    assert turn.prompt_tokens == 11
    assert turn.completion_tokens == 5
    assert turn.total_tokens == 16
    assert turn.has_tool_calls is True
    assert turn.tool_calls[0].call_id == "call-1"
    assert turn.tool_calls[0].arguments == {"repo_id": 3}


def test_openai_client_sends_model_messages_and_tools() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client(handler).complete(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="usr")],
        TOOLS,
    )

    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "gpt-4o-mini"
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_repository_profile"
    assert body["tool_choice"] == "auto"


def test_openai_client_omits_tools_when_none_are_offered() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client(handler).complete([ChatMessage(role="user", content="usr")], ())
    assert "tools" not in captured["body"]


def test_openai_client_maps_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json={"error": "slow down"})

    with pytest.raises(ModelClientError) as error:
        _client(handler).complete([], ())
    assert error.value.code == MODEL_RATE_LIMITED
    assert error.value.retryable is True


def test_openai_client_maps_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    with pytest.raises(ModelClientError) as error:
        _client(handler).complete([], ())
    assert error.value.code == MODEL_TIMEOUT


def test_openai_client_maps_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="boom")

    with pytest.raises(ModelClientError) as error:
        _client(handler).complete([], ())
    assert error.value.code == MODEL_REQUEST_FAILED


def test_openai_client_rejects_a_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="not json")

    with pytest.raises(ModelClientError) as error:
        _client(handler).complete([], ())
    assert error.value.code == MODEL_INVALID_RESPONSE


def test_openai_client_rejects_a_body_without_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ModelClientError) as error:
        _client(handler).complete([], ())
    assert error.value.code == MODEL_INVALID_RESPONSE


def test_openai_client_degrades_malformed_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {
                                        "name": "get_repository_profile",
                                        "arguments": "{not json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    turn = _client(handler).complete([], ())
    assert turn.tool_calls[0].arguments == {}


def test_openai_client_prices_a_turn_from_the_billing_table() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )

    turn = _priced_client(handler).complete([], ())
    # (1000 * 0.14 + 500 * 0.28) / 1_000_000 == 0.000280
    assert turn.cost == Decimal("0.000280")


def test_openai_client_leaves_cost_at_zero_without_prices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )

    turn = _client(handler).complete([], ())
    assert turn.cost == Decimal("0")


def test_a_truncated_response_still_reports_what_it_cost() -> None:
    """A cut-off answer is unusable, but it was billed, so its usage has to come back with it.

    Dropping the usage block when `finish_reason == "length"` made the tokens vanish from
    `agent_runs` and the budget walk backwards: a run could truncate repeatedly and never spend
    any of its allowance.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": '{"sections": ['}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )

    with pytest.raises(ModelClientError) as error:
        _priced_client(handler).complete([], ())

    assert error.value.code == MODEL_INVALID_RESPONSE
    assert error.value.prompt_tokens == 1000
    assert error.value.completion_tokens == 500
    assert error.value.cost == Decimal("0.000280")
