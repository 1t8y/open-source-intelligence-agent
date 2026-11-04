"""与供应商无关的聊天消息、工具 schema 与模型回复。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant", "tool"]

# 上下文块统一使用这些标签，任何供应商读到的都是同一批可机器解析的事实。
CANDIDATE_TAG = "candidate_repos"
TOPIC_TAG = "topic_id"
WINDOW_TAG = "research_window"

#: 把一条工具观测标记为「失败」而不是「结果」的顶层键。写侧
#: （`agent/graph.py`，负责渲染模型读到的那条观测）与读侧
#: （`agent/llm/offline.py:_collect_evidence`，负责从对话里重建
#: 研究快照）都走下面这两个函数，所以这个形状只有一处定义。
TOOL_ERROR_FIELD: Final[str] = "error"


def tool_error_payload(code: str, message: str) -> dict[str, Any]:
    """返回工具调用失败时回给模型的那条观测。

    失败被渲染成对象而不是文本，是因为模型要能按错误码行事：
    某个仓库上的 `TOOL_NOT_FOUND` 意味着「换一个试试」，而
    `TOOL_ACCESS_DENIED` 意味着「这个不该你来读」。由此带来的
    结果，也是它必须与判定函数放在一起的原因：失败与成功都是
    JSON 对象，只问一句「这是 dict 吗」的消费方分辨不出二者，
    会把一次失败的读取记成「读到了文档」的证据。
    """
    return {TOOL_ERROR_FIELD: {"code": code, "message": message}}


def is_tool_error_payload(payload: Any) -> bool:
    """判断一条观测是失败外壳，还是工具自己返回的数据。

    这里刻意按结构判断而不是按类型：离线适配器能看到的只有
    对话，所以「这次调用成功了吗」只能从记录下来的消息里回答。
    字段凑巧很多、本身就是 dict 的工具载荷是结果；失败外壳则是
    一个只装着 `error` 对象的 dict，而该对象带 `code`。
    """
    if not isinstance(payload, dict):
        return False
    error = payload.get(TOOL_ERROR_FIELD)
    return isinstance(error, dict) and "code" in error


class ModelToolCall(BaseModel):
    """模型请求发起的一次工具调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSchema(BaseModel):
    """提供给模型的一个白名单工具的 JSON schema 视图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ChatMessage(BaseModel):
    """由所有供应商适配器共用的单条规范化对话消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ChatRole
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


class ModelTurn(BaseModel):
    """一次模型回复，可能携带自由文本、请求的工具调用，或两者兼有。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")

    @property
    def has_tool_calls(self) -> bool:
        """返回这一轮回复是否要求调用至少一个工具。"""
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int:
        """返回 `prompt_tokens` 与 `completion_tokens` 之和。"""
        return self.prompt_tokens + self.completion_tokens
