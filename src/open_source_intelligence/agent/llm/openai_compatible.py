"""OpenAI-compatible chat completions adapter.

This adapter targets the `/chat/completions` shape shared by many vendors and gateways,
so switching provider is a configuration change rather than a code change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx

from .errors import (
    MODEL_INVALID_RESPONSE,
    MODEL_RATE_LIMITED,
    MODEL_REQUEST_FAILED,
    MODEL_TIMEOUT,
    ModelClientError,
)
from .messages import ChatMessage, ModelToolCall, ModelTurn, ToolSchema

DEFAULT_TIMEOUT_SECONDS = 60
RATE_LIMIT_STATUS = 429
#: Output ceiling for one completion. A reasoning model that also spends tokens on its chain of
#: thought needs an explicit cap on the *answer* length or it silently truncates a long JSON
#: draft mid-object, which the draft parser then rejects as "not a JSON object". This is large
#: enough for six full report sections and small enough to keep a stray run from burning budget.
DEFAULT_MAX_TOKENS = 8192


def _usage_field(usage: Any, key: str) -> int:
    """Read a non-negative token count, treating anything unexpected as zero."""
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """Decode tool-call arguments.

    Malformed arguments degrade to an empty mapping so the tool layer can reject them with
    a recorded `TOOL_INVALID_ARGUMENT` instead of aborting the whole run.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _encode_message(message: ChatMessage) -> dict[str, Any]:
    """Convert one normalized message into the wire format."""
    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id or "",
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


def _encode_tool(schema: ToolSchema) -> dict[str, Any]:
    """Convert one tool schema into the wire format."""
    return {
        "type": "function",
        "function": {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
    }


class OpenAICompatibleClient:
    """Call any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        input_price_per_mtok: Decimal = Decimal("0"),
        output_price_per_mtok: Decimal = Decimal("0"),
        client: httpx.Client | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model_name = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._input_price_per_mtok = input_price_per_mtok
        self._output_price_per_mtok = output_price_per_mtok
        self._client = client

    @property
    def provider(self) -> str:
        """Return the provider identifier recorded on the Agent config."""
        return "openai_compatible"

    @property
    def model(self) -> str:
        """Return the concrete model identifier."""
        return self._model_name

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """Send one chat completion request and normalize the response."""
        payload = self._build_payload(messages, tools)
        try:
            response = self._post(payload)
        except httpx.TimeoutException as exc:
            raise ModelClientError(MODEL_TIMEOUT, "model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelClientError(MODEL_REQUEST_FAILED, "model transport failed") from exc
        if response.status_code == RATE_LIMIT_STATUS:
            raise ModelClientError(MODEL_RATE_LIMITED, "model rate limit exceeded")
        if response.status_code >= 400:
            raise ModelClientError(
                MODEL_REQUEST_FAILED,
                f"model returned HTTP {response.status_code}",
            )
        return self._parse(response)

    def _build_payload(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> dict[str, Any]:
        """Build the request body, omitting tools when none are offered."""
        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": [_encode_message(message) for message in messages],
            "max_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = [_encode_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST the payload, reusing an injected client during tests."""
        url = f"{self._api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._client is not None:
            return self._client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        with httpx.Client(timeout=self._timeout_seconds) as client:
            return client.post(url, json=payload, headers=headers)

    def _parse(self, response: httpx.Response) -> ModelTurn:
        """Normalize a provider response into a `ModelTurn`."""
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise ModelClientError(MODEL_INVALID_RESPONSE, "model response was not JSON") from exc
        if not isinstance(body, dict):
            raise ModelClientError(MODEL_INVALID_RESPONSE, "model response was not an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelClientError(MODEL_INVALID_RESPONSE, "model response contained no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelClientError(MODEL_INVALID_RESPONSE, "model choice was not an object")
        # A response cut off by the output ceiling (`finish_reason: "length"`) is an incomplete
        # answer: a truncated JSON draft would otherwise reach the parser and fail downstream as
        # "not a JSON object", hiding the real cause. Surface it here while it is still a
        # transport-level fact.
        finish_reason = first.get("finish_reason")
        if finish_reason == "length":
            raise ModelClientError(
                MODEL_INVALID_RESPONSE,
                "model response was truncated by the output limit",
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelClientError(MODEL_INVALID_RESPONSE, "model choice carried no message")
        text = message.get("content")
        usage = body.get("usage")
        prompt_tokens = _usage_field(usage, "prompt_tokens")
        completion_tokens = _usage_field(usage, "completion_tokens")
        return ModelTurn(
            text=text if isinstance(text, str) else "",
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            model=self._model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=self._price_turn(prompt_tokens, completion_tokens),
        )

    def _price_turn(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        """Price one turn from the configured per-million-token rates.

        The provider reports token counts but no monetary amount, so the cost is computed here
        and flows into `agent_runs.estimated_cost` through the graph's accumulator. When no
        price is configured the turn costs zero, which keeps the offline default and any
        provider without a billing table from fabricating a number.
        """
        cost = (
            Decimal(prompt_tokens) * self._input_price_per_mtok
            + Decimal(completion_tokens) * self._output_price_per_mtok
        ) / Decimal(1_000_000)
        return cost.quantize(Decimal("0.000000"))

    @staticmethod
    def _parse_tool_calls(raw: Any) -> tuple[ModelToolCall, ...]:
        """Normalize the provider tool-call list, skipping unusable entries."""
        if not isinstance(raw, list):
            return ()
        calls: list[ModelToolCall] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            call_id = item.get("id")
            calls.append(
                ModelToolCall(
                    call_id=call_id if isinstance(call_id, str) and call_id else f"call-{index}",
                    name=name,
                    arguments=_decode_arguments(function.get("arguments")),
                )
            )
        return tuple(calls)
