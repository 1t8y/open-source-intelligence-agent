"""OpenAI 兼容的 Chat Completions 适配器：把统一消息契约译成请求与响应。

本适配器对准的是许多供应商与网关共用的 `/chat/completions` 形状，
因此换供应商只是改配置，而不是改代码。
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
#: 单次 completion 的输出上限。会额外把 token 花在思维链上的推理模型，
#: 必须对*答案*长度设一个显式上限，否则它会在对象中途悄悄截断一份长
#: JSON 草稿，而草稿解析器随后只会报「不是 JSON 对象」。这个值大到足以
#: 装下六个完整报告小节，小到能让失控的运行不至于烧穿预算。
DEFAULT_MAX_TOKENS = 8192


def _usage_field(usage: Any, key: str) -> int:
    """读取一个非负的 token 计数，遇到任何意外值一律按 0 处理。"""
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """解码工具调用参数。

    格式错误的参数降级为空映射，让工具层能用一条记录下来的
    `TOOL_INVALID_ARGUMENT` 拒绝它，而不是中断整次运行。
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
    """把一条规范化消息转换成线上格式。"""
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
    """把一个工具 schema 转换成线上格式。"""
    return {
        "type": "function",
        "function": {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
    }


class OpenAICompatibleClient:
    """调用任意 OpenAI 兼容的 chat completions 端点。"""

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
        """返回记录在 Agent 配置上的供应商标识。"""
        return "openai_compatible"

    @property
    def model(self) -> str:
        """返回具体的模型标识。"""
        return self._model_name

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """发送一次 chat completion 请求并归一化响应。"""
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
        """构造请求体；没有提供工具时省略 `tools`。"""
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
        """POST 请求体，测试期间复用注入的客户端。"""
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
        """把供应商响应归一化成 `ModelTurn`。"""
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
        # usage 块在任何形状检查之前读取，因为一份不能当草稿用的响应，
        # 仍然是供应商计过费的响应。下面每次抛出都带上它，被截断的调用
        # 才不会从本次运行的 token 与成本统计里消失。
        usage = body.get("usage")
        prompt_tokens = _usage_field(usage, "prompt_tokens")
        completion_tokens = _usage_field(usage, "completion_tokens")
        billed_cost = self._price_turn(prompt_tokens, completion_tokens)
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelClientError(
                MODEL_INVALID_RESPONSE,
                "model choice carried no message",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=billed_cost,
            )
        # 被输出上限截断的响应（`finish_reason: "length"`）是一份不完整的
        # 答案：否则被截断的 JSON 草稿会一路走到解析器，在下游以「不是
        # JSON 对象」的形式失败，掩盖真正的原因。趁它还是一个传输层事实，
        # 在这里就把它暴露出来。
        finish_reason = first.get("finish_reason")
        if finish_reason == "length":
            raise ModelClientError(
                MODEL_INVALID_RESPONSE,
                "model response was truncated by the output limit",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=billed_cost,
            )
        text = message.get("content")
        return ModelTurn(
            text=text if isinstance(text, str) else "",
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            model=self._model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=billed_cost,
        )

    def _price_turn(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        """按配置的「每百万 token」单价为这一轮计价。

        供应商只报 token 数、不报金额，所以成本在这里算出来，再经由图的
        累加器流进 `agent_runs.estimated_cost`。没有配置价格时这一轮成本
        为零，从而让离线默认值、以及任何没有价目表的供应商都不会凭空编出
        一个数字。
        """
        cost = (
            Decimal(prompt_tokens) * self._input_price_per_mtok
            + Decimal(completion_tokens) * self._output_price_per_mtok
        ) / Decimal(1_000_000)
        return cost.quantize(Decimal("0.000000"))

    @staticmethod
    def _parse_tool_calls(raw: Any) -> tuple[ModelToolCall, ...]:
        """归一化供应商的工具调用列表，跳过不可用的条目。"""
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
